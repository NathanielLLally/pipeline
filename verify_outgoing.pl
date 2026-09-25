use strict;
use warnings;
use Socket;
use Net::DNS::Resolver;

package Net::DNS::Resolver::Outgoing;
use base qw(Net::DNS::Resolver);
use Socket;

sub get_local_outgoing_ip {
    my ($self) = @_;
    
    # The issue is likely that connect() is returning immediately in a way 
    # that doesn't force the bind on this specific OS/Kernel.
    # We will try a set of targets and use a non-blocking connect 
    # and then check getsockname.
    
    my @targets = ('8.8.8.8', '1.1.1.1', '9.9.9.9');
    
    foreach my $target (@targets) {
        my $packed = inet_aton($target);
        next unless $packed;
        
        socket(my $sock, AF_INET, SOCK_STREAM, 0) or next;
        
        # Force the OS to pick an interface by attempting to connect.
        # On some systems, a simple connect() is enough to trigger the bind.
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

my $res = Net::DNS::Resolver::Outgoing->new();
my $ip = $res->get_local_outgoing_ip();
print "Detected Outgoing IP: " . ($ip // "undef") . "\n";

if ($ip && $ip ne '0.0.0.0' && $ip ne '127.0.0.1') {
    print "VERIFICATION SUCCESS: Public interface detected.\n";
} else {
    print "VERIFICATION FAILURE: Detected loopback or null IP ($ip).\n";
    exit 1;
}
