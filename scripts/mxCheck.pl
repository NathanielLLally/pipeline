#!/usr/bin/perl
#
# SMTP-level email verification: for each address, find the domain's MX, open an
# SMTP session to it, and ask whether the mailbox is deliverable -- without ever
# sending a message. The session is abandoned at RCPT TO, before DATA.
#
# This is the verification stage for leads.business_email. Addresses scraped from
# websites include long-dead mailboxes and typo'd domains; sending to those burns
# the sending domain's reputation, which is the one asset a cold-email pipeline
# cannot buy back. Verifying first is cheaper than being blocklisted.
#
# The false-positive guard matters as much as the check itself. Many MX hosts are
# catch-all: they accept RCPT TO for every local part, so a naive probe reports
# 100% verified and tells you nothing. Before testing the real address this asks
# for a random local part at the same domain; if the server accepts that too, the
# result for the real address is unusable and is reported as a failed check rather
# than as a verification.
#
# CPAN dependencies (cpanm):
#   cpanm Net::DNS Net::SMTP Parallel::ForkManager Try::Tiny JSON::PP
#
# Getopt::Long, Sys::Hostname, File::Temp and Time::HiRes are core, so nothing
# needs to be installed for them.
#
# Three further modules were specified for this script -- Net::DNS::Async,
# URI::Encode and Coro::AnyEvent -- and are deliberately not loaded, because the
# current implementation has no use for them: resolution is synchronous
# Net::DNS inside forked children, and there are no URLs to escape. Installing
# them and importing them unused would make the dependency list lie about what
# the script needs. They become real dependencies if concurrency is ever moved
# from process forking to an event loop, which is the likely next change here:
#
#   cpanm Net::DNS::Async URI::Encode Coro::AnyEvent
#
# Note that of the four, Coro::AnyEvent is the one not currently installed on
# this workstation, and Coro is the usual source of build trouble on a recent
# perl -- worth knowing before that refactor is attempted.
#
# Usage:
#   ./scripts/mxCheck.pl --email owner@example.com
#   ./scripts/mxCheck.pl --file addresses.txt --threads 30
#   ./scripts/mxCheck.pl --file addresses.txt --debug     # trace to STDERR
#
# Output is a JSON array on STDOUT. Debug tracing goes to STDERR only, so
# `--debug` never corrupts a piped result set.

use strict;
use warnings;
use utf8;

use Getopt::Long;
use JSON::PP;
use JSON::PP qw(encode_json decode_json);
use Net::SMTP;
use Net::DNS;
use Parallel::ForkManager;
use Try::Tiny;
use Sys::Hostname;
use File::Temp qw(tempfile);
use Time::HiRes qw(time);

# --- Configuration & Defaults ---
my $threads = 30;
my $email_arg;
my $file_arg;
my $debug;
my $help;

GetOptions(
    'email=s'   => \$email_arg,
    'file=s'    => \$file_arg,
    'threads=i' => \$threads,
    'debug'     => \$debug,
    'help'      => \$help,
) or die "Error in command line arguments\n";

if ($help || (!$email_arg && !$file_arg)) {
    print <<USAGE;
Usage: $0 [options]
Options:
  --email <email>    Verify a single email address
  --file <path>      Verify emails from a file (one per line)
  --threads <num>    Number of concurrent workers (default: $threads)
  --debug            Trace execution to STDERR, including a line before
                     every DNS and SMTP call naming host, port and payload
  --help             Show this help message
USAGE
    exit 0;
}

# --- Debug tracing ---------------------------------------------------------
#
# Everything below writes to STDERR, never STDOUT, so --debug is safe to use
# while piping JSON results into another process.
#
# Each line carries the elapsed time since start and the pid. The pid is what
# makes the output readable at all: with --threads 30 the children interleave,
# and without a pid column a trace is thirty conversations spliced together.
#
# The rule this file follows: no socket is opened, and no command is written to
# one, without a NET> line emitted first. The trace must be usable to answer
# "what was it about to talk to when it hung", which means the line has to
# precede the call that hangs, not follow it. Each NET> is paired with a NET<
# carrying the outcome and elapsed milliseconds.

my $T0 = time();

sub dbg {
    return unless $debug;
    my ($fmt, @args) = @_;
    my $msg = @args ? sprintf($fmt, @args) : $fmt;
    printf STDERR "[%8.3fs] [pid %6d] %s\n", time() - $T0, $$, $msg;
}

# Emitted immediately before a networking call. Returns the start time so the
# caller can hand it to net_done and report a duration.
sub net_start {
    my ($kind, $target, $detail) = @_;
    dbg("NET> %-12s %s%s", $kind, $target, defined $detail ? "  $detail" : "");
    return time();
}

sub net_done {
    my ($kind, $target, $started, $outcome) = @_;
    return unless $debug;
    # SMTP replies arrive with a trailing newline, and a multi-line reply (a 5.1.1
    # with a help URL, say) arrives with several. Folded onto one line the trace
    # stays greppable and one event stays one line.
    $outcome = defined $outcome ? $outcome : "ok";
    $outcome =~ s/\s+/ /g;
    $outcome =~ s/^\s+|\s+$//g;
    dbg("NET< %-12s %s  %.0fms  %s", $kind, $target,
        (time() - $started) * 1000, $outcome);
}

dbg("start: threads=%d hostname=%s perl=%vd", $threads, hostname, $^V);

# --- Input Gathering ---
my @emails_to_check;
if ($email_arg) {
    push @emails_to_check, $email_arg;
}
if ($file_arg) {
    dbg("reading addresses from %s", $file_arg);
    open(my $fh, '<', $file_arg) or die "Could not open file $file_arg: $!";
    while (my $line = <$fh>) {
        chomp $line;
        $line =~ s/^\s+|\s+$//g;
        push @emails_to_check, $line if $line;
    }
    close($fh);
}

dbg("%d address(es) queued", scalar @emails_to_check);

# --- Initialization ---
my $pm = Parallel::ForkManager->new($threads);
my @result_files;

if ($debug) {
    $pm->run_on_start(sub {
        my ($pid, $ident) = @_;
        dbg("fork: child pid=%d for %s", $pid, $ident);
    });
    $pm->run_on_finish(sub {
        my ($pid, $exit, $ident) = @_;
        dbg("reap: child pid=%d for %s exited %d", $pid, $ident, $exit);
    });
}

# Helper to get MX records. The resolver is passed in rather than shared from the
# parent: a Net::DNS::Resolver built before the fork hands every child the same
# UDP socket, and replies then land in whichever child reads first, so answers
# get attributed to the wrong domain. Each child builds its own.
sub get_mx_records {
    my ($dns, $host) = @_;
    my @mx_hosts;

    my $started = net_start("DNS MX", $host,
        sprintf("via %s", join(",", $dns->nameservers) || "system"));
    my $query = $dns->query($host, 'MX');
    net_done("DNS MX", $host, $started,
        $query ? "answer" : "no answer (" . $dns->errorstring . ")");

    if ($query) {
        foreach my $rr ($query->answer) {
            if ($rr->type eq 'MX') {
                push @mx_hosts, $rr;
                dbg("  mx %s pref %d", $rr->exchange, $rr->preference);
            }
        }
    }
    return @mx_hosts;
}

# Processing loop
foreach my $email (@emails_to_check) {
    # Create a temp file to store the result of this specific email
    my ($tfh, $tfname) = tempfile(LEGACY => 1, UNLINK => 0);
    close($tfh);
    push @result_files, $tfname;

    my $pid = $pm->start($email);
    if ($pid) {
        next; # Parent continues to next email
    }

    # --- Child Process ---
    dbg("checking %s (results -> %s)", $email, $tfname);

    my $result = {
        email => $email,
        verified => 0,
        error => undef,
        mx_server => undef,
    };

    my $dns = Net::DNS::Resolver->new;

    try {
        if ($email =~ /\@(.*)$/) {
            my $host = $1;
            my @mx_records = get_mx_records($dns, $host);

            if (!@mx_records) {
                $result->{error} = "no mx record for hostname $host";
                dbg("no MX for %s; giving up on %s", $host, $email);
            } else {
                my $rr = $mx_records[0];
                my $svr = lc $rr->exchange();
                $result->{mx_server} = $svr;

                my $started = net_start("SMTP connect", "$svr:25",
                    sprintf("HELO %s timeout 30s", hostname));
                my $smtp = Net::SMTP->new($svr,
                    Timeout => 30,
                    Hello => hostname,
                );
                net_done("SMTP connect", "$svr:25", $started,
                    $smtp ? "banner: " . ($smtp->banner // "(none)") : "connect failed");

                if (!$smtp) {
                    $result->{error} = "could not connect to smtp server $svr";
                } else {
                    # The catch-all guard. A random local part at the same domain
                    # is offered first; a server that accepts it accepts anything,
                    # so nothing can be concluded about the real address.
                    my @parts = reverse split(/\./, $host);
                    my $short_domain = sprintf("%s.%s", $parts[1], $parts[0]);
                    my $decoy = "adln12jqewfkjbrwgsdjh\@$short_domain";

                    $started = net_start("SMTP MAIL", $svr, "FROM:<$email>");
                    $smtp->mail($email);
                    net_done("SMTP MAIL", $svr, $started, scalar $smtp->message);

                    $started = net_start("SMTP RCPT", $svr,
                        "TO:<$decoy>  [catch-all probe]");
                    my $decoy_accepted = $smtp->to($decoy);
                    net_done("SMTP RCPT", $svr, $started,
                        ($decoy_accepted ? "ACCEPTED -> catch-all" : "rejected -> good")
                        . "  " . ($smtp->message // ""));

                    if ($decoy_accepted) {
                        $result->{error} = sprintf("false positive check failed for mx %s", $svr);
                    } else {
                        $started = net_start("SMTP RSET", $svr);
                        $smtp->reset;
                        net_done("SMTP RSET", $svr, $started, scalar $smtp->message);

                        $started = net_start("SMTP MAIL", $svr, "FROM:<$email>");
                        $smtp->mail($email);
                        net_done("SMTP MAIL", $svr, $started, scalar $smtp->message);

                        $started = net_start("SMTP RCPT", $svr,
                            "TO:<$email>  [real address]");
                        my $accepted = $smtp->to($email);
                        net_done("SMTP RCPT", $svr, $started,
                            ($accepted ? "ACCEPTED -> verified" : "rejected")
                            . "  " . ($smtp->message // ""));

                        if ($accepted) {
                            $result->{verified} = 1;
                        } else {
                            $result->{error} = $smtp->message();
                        }
                    }

                    # No DATA is ever sent: the session is torn down here, so the
                    # mailbox owner sees a connection and nothing else.
                    $started = net_start("SMTP QUIT", $svr);
                    $smtp->quit;
                    net_done("SMTP QUIT", $svr, $started);
                }
            }
        } else {
            $result->{error} = "invalid email format";
            dbg("rejected %s before any network call: no @ in address", $email);
        }
    } catch {
        $result->{error} = "exception: $_";
        dbg("exception checking %s: %s", $email, $_);
    };

    dbg("result %s: verified=%d mx=%s error=%s",
        $email, $result->{verified},
        $result->{mx_server} // "-", $result->{error} // "-");

    # Write result to temp file
    open(my $rfh, '>', $tfname) or die "Could not write result file $tfname: $!";
    print $rfh encode_json($result);
    close($rfh);

    $pm->finish;
}

dbg("all children dispatched; waiting");
$pm->wait_all_children;

# --- Aggregation ---
my @final_results;
my $json_encoder = JSON::PP->new->utf8;

foreach my $fname (@result_files) {
    if (-e $fname) {
        open(my $fh, '<', $fname) or next;
        my $content = <$fh>;
        close($fh);
        if ($content) {
            push @final_results, decode_json($content);
        } else {
            dbg("empty result file %s (child died before writing?)", $fname);
        }
        unlink $fname; # Clean up
    } else {
        dbg("missing result file %s", $fname);
    }
}

if ($debug) {
    my $ok = grep { $_->{verified} } @final_results;
    dbg("done: %d result(s), %d verified, %d failed, %.1fs elapsed",
        scalar @final_results, $ok, scalar(@final_results) - $ok, time() - $T0);
}

print $json_encoder->encode(\@final_results);
