use strict;
use warnings;
use Socket;
use Net::DNS::Resolver;

package Net::DNS::Resolver::Outgoing;
use base qw(Net::DNS::Resolver);
use Socket;

sub get_local_outgoing_ip {
    my ($self) = @_;
    my @nameservers = $self->nameservers();
    return undef unless @nameservers;

    my $ns = undef;
    foreach my $candidate (@nameservers) {
        if (inet_aton($candidate)) {
            $ns = $candidate;
            last;
        }
    }
    
    if (!$ns) {
        my $first = $nameservers[0];
        my $query = $self->query($first, 'A');
        if ($query && ref($query->answer) eq 'ARRAY' && $query->answer->[0]) {
            my $addr = $query->answer->[0]->address;
            if (inet_aton($addr)) {
                $ns = $addr;
            }
        }
    }

    return undef unless $ns;
    my $packed_ns = inet_aton($ns);

    socket(my $sock, AF_INET, SOCK_STREAM, 0) or return undef;
    
    eval {
        connect($sock, sockaddr_in(53, $packed_ns));
    };
    
    my $sockaddr = getsockname($sock);
    my $ip = undef;
    if ($sockaddr) {
        if (length($sockaddr) == 4) {
            $ip = inet_ntoa($sockaddr);
        } else {
            # Extract IP from sockaddr_in struct (typically offset 4)
            $ip = inet_ntoa(substr($sockaddr, 4, 4));
        }
    }
    close($sock);
    
    return $ip;
}

package main;

my $res = Net::DNS::Resolver::Outgoing->new();
my $outgoing_ip = $res->get_local_outgoing_ip();

if (!$outgoing_ip) {
    print "Could not determine outgoing IP\n";
    exit 1;
}

print "Outgoing IP: $outgoing_ip\n";

my $email = 'nate.lally@gmail.com';
my ($user, $domain) = split('@', $email);

print "Testing resolution for $domain...\n";
my $mx = $res->query($domain, 'MX');

if ($mx && ref($mx->answer) eq 'ARRAY' && $mx->answer->[0]) {
    my $mx_host = $mx->answer->[0]->exchange;
    print "Found MX: $mx_host\n";
    
    my $mx_ip_res = $res->query($mx_host, 'A');
    if ($mx_ip_res && ref($mx_ip_res->answer) eq 'ARRAY' && $mx_ip_res->answer->[0]) {
        print "MX IP: " . $mx_ip_res->answer->[0]->address . "\n";
    }
} else {
    print "MX lookup failed\n";
}
