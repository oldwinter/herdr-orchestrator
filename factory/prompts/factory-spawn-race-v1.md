abort() fired during check spawn — between Popen() returning and the
process landing in the live set — must still kill the check's whole
process group. A post-registration stop recheck makes the interleave
airtight: either abort snapshots the live process, or the dispatcher
sees the stop flag and kills the just-registered group itself.
