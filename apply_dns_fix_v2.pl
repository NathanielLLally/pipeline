use strict;
use warnings;

my $file = 'scripts/mxCheck.pl';
open(my $fh, '<', $file) or die $!;
my @lines = <$fh>;
close($fh);

my $new_func = <<'FUNC';
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
