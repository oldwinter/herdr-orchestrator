Checks run in their own session/process group. A check that spawns a
descendant writing a marker after 1.5s must fail the item with
factory_check_timeout AND the descendant must never write the marker;
an identical unchecked run must let the marker appear (control); a check
trapping SIGTERM and exiting 0 must still record factory_check_timeout;
SIGINT to the runner exits 130 promptly and kills the in-flight check's
descendant before its write lands.
