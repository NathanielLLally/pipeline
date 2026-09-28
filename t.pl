#!/usr/bin/perl
if (-t STDIN) {
    print "Interactive terminal (waiting for human keyboard input).\n";
} else {
    print "Data is being piped or redirected into STDIN.\n";
}

if (-p STDIN) {
    print "STDIN is connected to a pipe.\n";
} else {
  print "nope\n";
}

