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
    # select the public WAN interface.
    my $public_probe_ip = '8.8.8.8';
    my $packed_ns = inet_aton($public_probe_ip);

    socket(my $sock, AF_INET, SOCK_STREAM, 0) or return undef;
    
    # Set a short timeout for the connect to avoid hanging, but 
    # we just need the OS to assign the local interface.
    eval {
        connect($sock, sockaddr_in(53, $packed_ns));
    };
    
    my $sockaddr = getsockname($sock);
    my $ip = undef;
    if ($sockaddr) {
        my $raw_ip = (length($sockaddr) == 4) ? $sockaddr : substr($sockaddr, 4, 4);
        $ip = inet_ntoa($raw_ip);
    }
    close($sock);
    
    # If we got 0.0.0.0, the bind didn't happen. Try a different public target.
    if (defined $ip && $ip eq '0.0.0.0') {
        $packed_ns = inet_aton('1.1.1.1');
        socket(my $sock2, AF_INET, SOCK_STREAM, 0) or return undef;
        eval { connect($sock2, sockaddr_in(53, $packed_ns)); };
        my $sockaddr2 = getsockname($sock2);
        if ($sockaddr2) {
            my $raw_ip2 = (length($sockaddr2) == 4) ? $sockaddr2 : substr($sockaddr2, 4, 4);
            $ip = inet_ntoa($raw_ip2);
        }
        close($sock2);
    }
    
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
