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
#   cpanm Net::DNS Net::SMTP Parallel::ForkManager Try::Tiny JSON::PP IO::Socket::Socks
#   cpanm LWP::UserAgent LWP::Protocol::https
#
# Getopt::Long, Sys::Hostname, File::Temp, Time::HiRes and Net::Cmd are core, so
# nothing needs to be installed for them.
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
# IO::Socket::Socks::Wrapper (the usual way to make Net::SMTP proxy-transparent)
# was tried and rejected: 3 of its own 57 test-suite assertions fail on this
# workstation, and it produced "Bad file descriptor" against a re-blessed
# Net::SMTP object rather than a working connection. --socks5-proxy instead
# opens the tunnel with IO::Socket::Socks directly and re-blesses the resulting
# socket into Net::SMTP by hand -- the same thing Net::SMTP::new() does
# internally right after its own TCP connect. This was verified working
# end-to-end against smtp.google.com through both the project's Webshare pool
# and an ssh -D tunnel; see the SOCKS5 section below for what those two
# transports can and cannot reach.
#
# Usage:
#   ./scripts/mxCheck.pl --email owner@example.com
#   ./scripts/mxCheck.pl --file addresses.txt --threads 30
#   ./scripts/mxCheck.pl --file addresses.txt --debug     # trace to STDERR
#   ./scripts/mxCheck.pl --file addresses.txt --rate-limit 5
#   ./scripts/mxCheck.pl --file addresses.txt --socks5-proxy host:port
#
# Output is a JSON array on STDOUT. Debug tracing goes to STDERR only, so
# `--debug` never corrupts a piped result set.
#
# --- Rate limiting ---------------------------------------------------------
#
# --rate-limit N caps new checks *started* to N per second, summed across all
# worker children, not per worker. It is enforced in the parent process, in
# the loop that calls Parallel::ForkManager->start -- there is no way to
# coordinate a shared budget across already-forked children without IPC, but
# each check opens its SMTP connection within a few ms of being started, so
# throttling starts is throttling connections closely enough for this purpose.
# --threads still bounds how many checks run concurrently; --rate-limit bounds
# how fast new ones begin. Omit it (or pass 0) for the old unthrottled
# behaviour. A receiving mail server that sees a burst of RCPT TOs from one IP
# is the thing this exists to avoid -- it is indistinguishable from directory
# harvesting and is what gets a sending IP blocklisted.
#
# --- SOCKS5 proxy support ---------------------------------------------------
#
# --socks5-proxy host:port routes the SMTP TCP connection (not the DNS MX
# lookup, which always resolves directly) through a SOCKS5 proxy. Credentials
# are read from MXCHECK_SOCKS5_USER / MXCHECK_SOCKS5_PASS in the environment,
# not from --socks5-user/--socks5-pass on the command line, because a CLI
# argument is visible to every other process on the box via `ps aux` and is
# written to shell history -- the same class of leak that put a live Postgres
# password into a public repo earlier in this project. --socks5-user/-pass
# exist only as an override for local testing and print a warning when used.
#
# Two things worth knowing before pointing this at a pool, both measured
# 2026-09-21:
#   - The project's Webshare pool (PROXY_LIST_URL in .env) refuses the CONNECT
#     for ports 25, 465 and 587 outright ("Not allowed" -- a proxy-side ACL,
#     not a network failure); it works fine for 80/443/8080. It cannot be used
#     for SMTP verification as configured.
#   - All three scraper hosts (worker/worker2/worker3.accurateleadinfo.com)
#     also cannot reach port 25 outbound at all, proxy or no proxy -- this is
#     standard hosting-provider anti-spam policy, confirmed by testing
#     `/dev/tcp/smtp.google.com/25` directly from each host. An `ssh -D` tunnel
#     to any of them inherits that block. Only this workstation's own IP has
#     been confirmed to reach port 25 today.
#   --socks5-proxy is therefore built and tested against a working target
#   (an `ssh -D` tunnel to a host that itself permits outbound 25, or a
#   commercial proxy that does not filter mail ports) rather than against
#   anything already in this project's inventory.

use strict;
use warnings;
use utf8;

use Getopt::Long;
use JSON::PP;
use JSON::PP qw(encode_json decode_json);
use Net::SMTP;
use Net::DNS;
use Net::DNS::Packet;
use Net::Cmd ();
use Parallel::ForkManager;
use Try::Tiny;
use Sys::Hostname;
use File::Temp qw(tempfile);
use Time::HiRes qw(time sleep);
use IO::Socket::Socks ();
use LWP::UserAgent;
use Socket qw(inet_aton inet_ntoa sockaddr_in unpack_sockaddr_in inet_pton inet_ntop);

# --- DNS Outgoing IP Validation Subclass ---
#
# This subclass of Net::DNS::Resolver provides a method to determine the local
# interface IP address that the OS will use to route DNS queries. This is used
# for pre-flight QC to ensure the script isn't running on a misconfigured host
# (e.g. routing through localhost or the wrong public interface).
package Net::DNS::Resolver::Outgoing;
use base qw(Net::DNS::Resolver);
use Socket;

sub get_local_outgoing_ip {
    my ($self) = @_;

    # We MUST probe remote public IPs to force the OS to select the public WAN interface.
    # Using system nameservers often returns a local loopback or gateway IP.
    my @targets = ('8.8.8.8', '1.1.1.1', '9.9.9.9');
    
    foreach my $target (@targets) {
        my $packed = inet_aton($target);
        next unless $packed;
        
        socket(my $sock, AF_INET, SOCK_STREAM, 0) or next;
        
        eval {
            connect($sock, sockaddr_in(53, $packed));
        };
        
        my $sockaddr = getsockname($sock);
        if ($sockaddr) {
            my $raw_ip = (length($sockaddr) == 4) ? $sockaddr : substr($sockaddr, 4, 4);
            my $ip = inet_ntoa($raw_ip);
            close($sock);
            return $ip if $ip && $ip ne '0.0.0.0';
        }
        close($sock);
    }
    
    return undef;
}

package main;

my $threads = 30;
my $email_arg;
my $file_arg;
my $debug;
my $help;
my $rate_limit = 0;        # checks started per second, 0 = unthrottled
my $socks5_proxy;          # "host:port"
my $socks5_user_cli;       # override for local testing only; see header note
my $socks5_pass_cli;
my $force_check = 0;

GetOptions(
    'email=s'        => \$email_arg,
    'file=s'         => \$file_arg,
    'threads=i'      => \$threads,
    'debug'          => \$debug,
    'rate-limit=f'   => \$rate_limit,
    'socks5-proxy=s' => \$socks5_proxy,
    'socks5-user=s'  => \$socks5_user_cli,
    'socks5-pass=s'  => \$socks5_pass_cli,
    'help'           => \$help,
    'force-check'      => \$force_check,
) or die "Error in command line arguments\n";


if ($help || (!$email_arg && !$file_arg)) {
    print <<USAGE;
Usage: $0 [options]
Options:
  --email <email>    Verify a single email address
  --file <path>      Verify emails from a file (one per line)
  --threads <num>    Number of concurrent workers (default: $threads)
  --rate-limit <n>   Max checks started per second, summed across all
                     workers (default: 0, unthrottled)
  --socks5-proxy <host:port>
                     Route the SMTP connection through a SOCKS5 proxy.
                     Credentials come from MXCHECK_SOCKS5_USER /
                     MXCHECK_SOCKS5_PASS in the environment; omit both for
                     an unauthenticated proxy.
  --debug            Trace execution to STDERR, including a line before
                     every DNS and SMTP call naming host, port and payload
  --help             Show this help message
  --force-check       Continue if reverse DNS fails (use IP literal HELO)
USAGE
    exit 0;
}

if ($socks5_proxy && $socks5_proxy !~ /^(.+):(\d+)$/) {
    die "--socks5-proxy must be host:port, got '$socks5_proxy'\n";
}
if ($socks5_proxy) {
  die "implement sanity checks on proxy for tcp connect to port 25, rev dns ptr on ip, honey pot and tamper checks before using";
}
my ($socks5_host, $socks5_port) = $socks5_proxy ? ($1, $2) : ();

# Credentials: environment first, CLI override last and loudly. A password on
# the command line is visible to every other local user via `ps aux` and lands
# in shell history -- see the header note.
my $socks5_user = $ENV{MXCHECK_SOCKS5_USER};
my $socks5_pass = $ENV{MXCHECK_SOCKS5_PASS};
if (defined $socks5_user_cli || defined $socks5_pass_cli) {
    warn "[mxCheck] WARNING: --socks5-user/--socks5-pass put credentials in " .
         "`ps aux` and shell history. Use MXCHECK_SOCKS5_USER / " .
         "MXCHECK_SOCKS5_PASS instead except for local testing.\n";
    $socks5_user = $socks5_user_cli if defined $socks5_user_cli;
    $socks5_pass = $socks5_pass_cli if defined $socks5_pass_cli;
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

dbg("start: threads=%d rate_limit=%s socks5=%s hostname=%s perl=%vd",
    $threads, ($rate_limit > 0 ? "${rate_limit}/s" : "unthrottled"),
    ($socks5_proxy // "(direct)"), hostname, $^V);

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

# Get the local outgoing IP address from a socket handle. Returns the IP as a
# dotted quad string, or undef on error.
sub get_socket_local_ip {
    my ($sock) = @_;
    try {
        my $sockaddr = $sock->sockname();
        return undef unless $sockaddr;
        # Extract the 4-byte IP from the sockaddr_in structure (offset 4 on Linux)
        my $raw_ip = (length($sockaddr) == 4) ? $sockaddr : substr($sockaddr, 4, 4);
        return inet_ntoa($raw_ip);
    } catch {
        dbg("  could not get socket local ip: %s", $_);
        return undef;
    };
}

# Do a reverse DNS lookup on an IP address using Google nameservers (8.8.8.8, 8.8.4.4).
# Supports both IPv4 and IPv6. Returns the hostname if found, or the IP address itself if
# the lookup fails or returns no result. Reports which nameserver and IP version was used.
sub reverse_dns_lookup {
    my ($ip, $dns_unused) = @_;
    return $ip unless $ip;

    my $ip_version;
    my $reverse_host;

    # Detect IPv4 (a.b.c.d)
    if ($ip =~ /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/) {
        my @octets = ($1, $2, $3, $4);
        return $ip if grep { $_ > 255 } @octets;  # Invalid octets
        $reverse_host = join(".", reverse(@octets), "in-addr", "arpa");
        $ip_version = "IPv4";
    }
    # Detect IPv6 (hex with colons)
    elsif ($ip =~ /:[0-9a-fA-F]/ || $ip =~ /::/) {
        # Expand and normalize IPv6
        my $normalized_ip = normalize_ipv6($ip);
        if (!$normalized_ip) {
            dbg("  reverse dns %s: invalid IPv6 format, falling back to %s", $ip, $ip);
            return $ip;
        }
        # Convert to reverse DNS: remove colons and reverse nibbles
        my @nibbles = split(//, $normalized_ip);
        @nibbles = reverse @nibbles;
        $reverse_host = join(".", @nibbles, "ip6", "arpa");
        $ip_version = "IPv6";
    }
    else {
        dbg("  reverse dns %s: unrecognized format, falling back to %s", $ip, $ip);
        return $ip;
    }

    # Create a new resolver pointing to Google nameservers
    my $google_dns = Net::DNS::Resolver->new;
    $google_dns->nameservers('8.8.8.8', '8.8.4.4');

    my $started = net_start("DNS PTR", $reverse_host,
        sprintf("reverse lookup for %s (%s) via 8.8.8.8, 8.8.4.4", $ip, $ip_version));
    my $query = $google_dns->query($reverse_host, 'PTR');
    net_done("DNS PTR", $reverse_host, $started,
        $query ? "answer" : "no answer (" . $google_dns->errorstring . ")");

    if ($query) {
        foreach my $rr ($query->answer) {
            if ($rr->type eq 'PTR') {
                my $hostname = $rr->ptrdname;
                $hostname =~ s/\.$//;  # Remove trailing dot if present
                dbg("  reverse dns %s -> %s (%s, via Google DNS)", $ip, $hostname, $ip_version);
                return $hostname;
            }
        }
    }
    dbg("  reverse dns %s: no PTR record found (%s), falling back to %s", $ip, $ip_version, $ip);
    return $ip;
}

# Normalize IPv6 address to full hex notation (32 hex characters, no colons).
# Takes a compact IPv6 like "2001:db8::1" and returns "20010db8000000000000000000000001".
# Returns undef if the input is not a valid IPv6.
sub normalize_ipv6 {
    my ($ip) = @_;

    # Handle IPv6 with ::
    if ($ip =~ /::/) {
        my ($left, $right) = split(/::/, $ip);
        my @left_parts = $left ? split(/:/, $left) : ();
        my @right_parts = $right ? split(/:/, $right) : ();

        # The total must be <= 8 groups
        if (scalar(@left_parts) + scalar(@right_parts) >= 8) {
            return undef;  # Invalid
        }

        my $gap = 8 - scalar(@left_parts) - scalar(@right_parts);
        my @parts = (@left_parts, ("0000") x $gap, @right_parts);

        my $result = '';
        foreach my $part (@parts) {
            my $hex = sprintf("%04x", hex($part));
            return undef unless $hex =~ /^[0-9a-f]{4}$/;
            $result .= $hex;
        }
        return $result;
    }
    else {
        # No ::, just split and pad each group
        my @parts = split(/:/, $ip);
        return undef unless @parts == 8;

        my $result = '';
        foreach my $part (@parts) {
            my $hex = sprintf("%04x", hex($part));
            return undef unless $hex =~ /^[0-9a-f]{4}$/;
            $result .= $hex;
        }
        return $result;
    }
}

# Opens an SMTP session, either directly or (when --socks5-proxy is set)
# tunneled through a SOCKS5 proxy. Returns a connected, blessed Net::SMTP
# object on success, or undef.
#
# The proxied path does not use Net::SMTP->new(), because Net::SMTP has no
# hook for handing it an already-open socket and there is no clean way to
# make its own connect() go through a proxy. Instead the SOCKS tunnel is
# opened with IO::Socket::Socks directly, then the resulting handle is
# re-blessed into Net::SMTP and its connection-open bookkeeping
# (net_smtp_arg, net_smtp_host, the initial banner read, autoflush, debug)
# is redone by hand -- this mirrors exactly what Net::SMTP::new() does
# immediately after its own SUPER::new() TCP connect succeeds. Everything
# after that (mail/to/reset/quit) is unmodified Net::SMTP and works
# identically on both paths.
#
sub smtp_connect {
    my ($svr, $dns, $helo_host) = @_;

    if (!$socks5_proxy) {
        # For direct connections
        my $sock = Net::SMTP->new($svr, Timeout => 30, Hello => $helo_host);
        return $sock;
    }

    # SOCKS5 path
    my %sockopt = (
        ProxyAddr   => $socks5_host,
        ProxyPort   => $socks5_port,
        ConnectAddr => $svr,
        ConnectPort => 25,
        SocksVersion => 5,
        Timeout     => 30,
    );
    if (defined $socks5_user && length $socks5_user) {
        $sockopt{AuthType} = 'userpass';
        $sockopt{Username} = $socks5_user;
        $sockopt{Password} = $socks5_pass;
    }

    my $sock = IO::Socket::Socks->new(%sockopt);
    if (!$sock) {
        dbg("  socks5 tunnel to %s via %s failed: %s",
            "$svr:25", $socks5_proxy, $IO::Socket::Socks::SOCKS_ERROR // "?");
        return undef;
    }

    bless $sock, 'Net::SMTP';
    ${*$sock}{net_smtp_arg}  = { Timeout => 30 };
    ${*$sock}{net_smtp_host} = $svr;
    $sock->autoflush(1);
    $sock->debug(0);

    if ($sock->response() != Net::Cmd::CMD_OK()) {
        dbg("  smtp banner read failed after socks5 connect to %s: %s %s",
            $svr, $sock->code, $sock->message);
        $sock->close;
        return undef;
    }
    (${*$sock}{net_smtp_banner}) = $sock->message;
    (${*$sock}{net_smtp_domain}) = $sock->message =~ /\A\s*(\S+)/;

    unless ($sock->hello($helo_host)) {
        dbg("  HELO %s failed after socks5 connect to %s: %s", $helo_host, $svr, $sock->message);
        $sock->close;
        return undef;
    }
    dbg("  HELO sent as %s", $helo_host);

    return $sock;
}

# Rate limiter state, kept in the parent only: it governs how fast new checks
# are *started*, not what each child does once running, so it lives in the
# dispatch loop below rather than anywhere a forked child could see it.
my $rl_window_start = time();
my $rl_count_in_window = 0;

# Blocks the parent, if necessary, so that starts stay under --rate-limit
# checks per second. A no-op when --rate-limit is 0.
sub rate_limit_wait {
    return if $rate_limit <= 0;

    my $now = time();
    if ($now - $rl_window_start >= 1) {
        $rl_window_start = $now;
        $rl_count_in_window = 0;
    }
    if ($rl_count_in_window >= $rate_limit) {
        my $wait = 1 - ($now - $rl_window_start);
        if ($wait > 0) {
            dbg("rate-limit: %d/%s started this window, sleeping %.3fs",
                $rl_count_in_window, $rate_limit, $wait);
            sleep($wait);
        }
        $rl_window_start = time();
        $rl_count_in_window = 0;
    }
    $rl_count_in_window++;
}

# Per-domain rate limiter: enforces 1 check per second per unique email domain.
# This is independent of the global rate limit and is meant to avoid being too
# aggressive to any single receiving mail server. Tracked in the parent only,
# so it persists across all forks.
my %domain_last_connect_time;

sub domain_rate_limit_wait {
    my ($domain) = @_;
    return unless $domain;

    my $now = time();
    my $last = $domain_last_connect_time{$domain};

    if (defined $last && $now - $last < 1) {
        my $wait = 1 - ($now - $last);
        if ($wait > 0) {
            dbg("domain-rate-limit: %s, sleeping %.3fs", $domain, $wait);
            sleep($wait);
        }
    }

    $domain_last_connect_time{$domain} = time();
}

# --- Pre-flight WAN identity -----------------------------------------------
#
# The HELO name is decided once, here, before any MX lookup or SMTP connect,
# and then handed to every child.
#
# It cannot be derived from the local socket: a socket probe reports the
# address the kernel would bind to, which on any NATed host (this workstation
# included -- 172.20.10.3) is an RFC1918 address that no receiving MX will
# ever see. What the remote sees is the public egress address, so that is what
# has to be asked for, from something outside the NAT. Two echo services are
# tried in order; the first that answers wins.
#
# RFC 5321 wants a HELO that is either an FQDN resolving back to the sender or
# an address literal in brackets. If the public address has a PTR, that PTR is
# the HELO. If it does not, an honest address literal is still conformant, but
# it is also the single strongest spam signal a receiving MX can see, so the
# default is to stop rather than to quietly burn the IP's reputation.
# --force-check opts into it deliberately.
my $wan_ip;
{
    my $ua = LWP::UserAgent->new(timeout => 10, agent => 'mxCheck/1.0');
    # ifconfig.me's bare root serves the HTML page to anything that does not
    # look like curl; /ip is the plain-text endpoint and is what is wanted here.
    foreach my $url ('https://ifconfig.me/ip', 'https://ipecho.net/plain') {
        my $started = net_start("HTTP GET", $url, "public ip lookup");
        my $res = $ua->get($url);
        my $body = $res->is_success ? ($res->decoded_content // '') : '';
        $body =~ s/^\s+|\s+$//g;
        # An echo service that decides to serve its HTML page instead of a bare
        # address would otherwise dump the whole document into the trace.
        net_done("HTTP GET", $url, $started,
            $res->is_success ? substr($body, 0, 80) : $res->status_line);
        next unless $res->is_success;
        if ($body =~ /^[0-9a-fA-F:.]+$/) {
            $wan_ip = $body;
            last;
        }
    }
}
die "mxCheck: could not determine this host's public IP from ifconfig.me or " .
    "ipecho.net/plain. Without it there is no way to choose a legitimate HELO " .
    "name; refusing to connect.\n" unless $wan_ip;

# reverse_dns_lookup returns its argument unchanged when there is no PTR, so
# "the answer is the IP we asked about" is how a missing PTR is detected.
my $wan_ptr = reverse_dns_lookup($wan_ip);
my $wan_helo;
if ($wan_ptr && $wan_ptr ne $wan_ip) {
    $wan_helo = $wan_ptr;
    dbg("pre-flight: public ip %s has PTR %s; HELO will be %s",
        $wan_ip, $wan_ptr, $wan_helo);
} elsif ($force_check) {
    # RFC 5321 s4.1.3: an IPv6 address literal carries an "IPv6:" tag inside
    # the brackets. An IPv4 literal is bare. Getting this wrong produces a
    # syntactically invalid HELO, which is worse than no PTR.
    $wan_helo = ($wan_ip =~ /:/) ? "[IPv6:$wan_ip]" : "[$wan_ip]";
    warn "[mxCheck] no PTR record for public IP $wan_ip; --force-check given, " .
         "opening with the RFC 5321 address literal HELO $wan_helo\n";
    dbg("pre-flight: no PTR for %s; --force-check, HELO will be %s",
        $wan_ip, $wan_helo);
} else {
    die "mxCheck: public IP $wan_ip has no PTR record. A HELO from an " .
        "unresolvable address is rejected or greylisted by most receiving MX " .
        "hosts and damages this IP's sending reputation. Fix the reverse DNS, " .
        "or re-run with --force-check to open with the address literal " .
        "HELO " . (($wan_ip =~ /:/) ? "[IPv6:$wan_ip]" : "[$wan_ip]") . ".\n";
}

# Processing loop
foreach my $email (@emails_to_check) {
    rate_limit_wait();

    # Extract domain and apply per-domain rate limiting (1 check/second per domain)
    my ($domain) = $email =~ /\@(.*)$/;
    domain_rate_limit_wait($domain) if $domain;

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
                    sprintf("HELO %s timeout 30s%s", $wan_helo,
                        $socks5_proxy ? "  via socks5 $socks5_proxy" : ""));
                my $smtp = smtp_connect($svr, $dns, $wan_helo);
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
