#!/usr/bin/perl

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

# --- Configuration & Defaults ---
my $threads = 30;
my $email_arg;
my $file_arg;
my $help;

GetOptions(
    'email=s'   => \$email_arg,
    'file=s'    => \$file_arg,
    'threads=i' => \$threads,
    'help'      => \$help,
) or die "Error in command line arguments\n";

if ($help || (!$email_arg && !$file_arg)) {
    print <<USAGE;
Usage: $0 [options]
Options:
  --email <email>    Verify a single email address
  --file <path>      Verify emails from a file (one per line)
  --threads <num>     Number of concurrent workers (default: $threads)
  --help             Show this help message
USAGE
    exit 0;
}

# --- Input Gathering ---
my @emails_to_check;
if ($email_arg) {
    push @emails_to_check, $email_arg;
}
if ($file_arg) {
    open(my $fh, '<', $file_arg) or die "Could not open file $file_arg: $!";
    while (my $line = <$fh>) {
        chomp $line;
        $line =~ s/^\s+|\s+$//g;
        push @emails_to_check, $line if $line;
    }
    close($fh);
}

# --- Initialization ---
my $pm = Parallel::ForkManager->new($threads);
my $dns = Net::DNS::Resolver->new;
my @result_files;

# Helper to get MX records
sub get_mx_records {
    my ($dns, $host) = @_;
    my @mx_hosts;
    my $query = $dns->query($host, 'MX');
    if ($query) {
        foreach my $rr ($query->answer) {
            if ($rr->type eq 'MX') {
                push @mx_hosts, $rr;
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
    my $result = {
        email => $email,
        verified => 0,
        error => undef,
        mx_server => undef,
    };

    try {
        if ($email =~ /\@(.*)$/) {
            my $host = $1;
            my @mx_records = get_mx_records($dns, $host);

            if (!@mx_records) {
                $result->{error} = "no mx record for hostname $host";
            } else {
                my $rr = $mx_records[0];
                my $svr = lc $rr->exchange();
                $result->{mx_server} = $svr;

                my $smtp = Net::SMTP->new($svr,
                    Timeout => 30,
                    Hello => hostname,
                );

                if (!$smtp) {
                    $result->{error} = "could not connect to smtp server $svr";
                } else {
                    # False positive check
                    my $domain = $host;
                    # Basic domain extraction for dummy check
                    if ($domain =~ /^(.*)\..*$/) {
                        # We just need a dummy address at the same domain
                        # Original code used "adln12jqewfkjbrwgsdjh\@$domain"
                        # where $domain was extracted as first two parts.
                        # Let's keep the original intent.
                    }

                    # To mirror original: get domain parts
                    my @parts = reverse split(/\./, $host);
                    my $short_domain = sprintf("%s.%s", $parts[1], $parts[0]);

                    $smtp->mail($email);
                    if ($smtp->to("adln12jqewfkjbrwgsdjh\@$short_domain")) {
                        $result->{error} = sprintf("false positive check failed for mx %s", $svr);
                    } else {
                        $smtp->reset;
                        $smtp->mail($email);
                        if ($smtp->to($email)) {
                            $result->{verified} = 1;
                        } else {
                            $result->{error} = $smtp->message();
                        }
                    }
                    $smtp->quit;
                }
            }
        } else {
            $result->{error} = "invalid email format";
        }
    } catch {
        $result->{error} = "exception: $_";
    };

    # Write result to temp file
    open(my $rfh, '>', $tfname) or die "Could not write result file $tfname: $!";
    print $rfh encode_json($result);
    close($rfh);

    $pm->finish;
}

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
        }
        unlink $fname; # Clean up
    }
}

print $json_encoder->encode(\@final_results);
