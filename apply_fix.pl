use strict;
use warnings;

my $file = 'scripts/mxCheck.pl';
open(my $fh, '<', $file) or die $!;
my @lines = <$fh>;
close($fh);

my $new_func = <<'FUNC';
sub get_local_outgoing_ip {
    my ($self) = @_;

    # We MUST probe a remote public IP (like Google DNS) to force the OS to 
    # select the public WAN interface. Using system nameservers often 
    # returns a local loopback or gateway IP (e.g. 127.0.0.1 or 192.168.x.x)
    # which does not represent the IP the rest of the world sees.
    my $public_probe_ip = '8.8.8.8';
    my $packed_ns = inet_aton($public_probe_ip);

    # Explicitly use AF_INET to ensure we get a 4-byte address
    socket(my $sock, AF_INET, SOCK_STREAM, 0) or return undef;
    
    eval {
        # We don't need a successful connect; getsockname returns the local bind
        # determined by the kernel's routing table for this destination.
        connect($sock, sockaddr_in(53, $packed_ns));
    };
    
    my $sockaddr = getsockname($sock);
    my $ip = undef;
    if ($sockaddr) {
        # Extract the 4-byte IP from the sockaddr_in structure (offset 4 on Linux)
        my $raw_ip = (length($sockaddr) == 4) ? $sockaddr : substr($sockaddr, 4, 4);
        $ip = inet_ntoa($raw_ip);
    }
    close($sock);
    
    return $ip;
}
FUNC

my $start = -1;
my $end = -1;

for (my $i = 0; $i < scalar @lines; $i++) {
    if ($lines[$i] =~ /sub get_local_outgoing_ip \{/) {
        $start = $i;
    }
    if ($start != -1 && $lines[$i] =~ /^\}/) {
        $end = $i;
        last;
    }
}

if ($start != -1 && $end != -1) {
    splice(@lines, $start, ($end - $start + 1), $new_func);
    open(my $out, '>', $file) or die $!;
    print $out join('', @lines);
    close($out);
    print "Successfully replaced function\n";
} else {
    print "Could not find function boundaries\n";
    exit 1;
}
