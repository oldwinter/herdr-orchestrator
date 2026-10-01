Pin transitive dependency release ordering.

Contract:
- A three-level requires chain (alpha -> beta -> gamma) releases one
  level per intake+drain cycle; each level is enqueued only after its
  predecessor reaches succeeded.
