sub get_local_outgoing_ip {
    my ($self) = @_;
    my @nameservers = $self->nameservers();
    return undef unless @nameservers;

    # 1. Find the first IPv4 nameserver to avoid AF_INET6 length issues with inet_ntoa
    my $ns = undef;
    foreach my $candidate (@nameservers) {
        if (inet_aton($candidate)) {
            $ns = $candidate;
            last;
        }
