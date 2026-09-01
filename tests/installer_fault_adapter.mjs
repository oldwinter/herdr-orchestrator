import {
  existsSync,
  mkdirSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

const ADAPTER = Symbol.for("herdr-orchestrator.installer-test-adapter.v1");
const WAIT_ARRAY = new Int32Array(new SharedArrayBuffer(4));
let durableMutationCount = 0;

function requestedMutation(name) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value > 0 ? value : null;
}

globalThis[ADAPTER] = {
  beforeJournalClaim() {
    if (
      process.env.HERDR_ORCHESTRATOR_TEST_FAIL_ON_JOURNAL_CLAIM === "1"
    ) {
      throw new Error("installer_test_unexpected_journal_claim");
    }
    const barrier = process.env.HERDR_ORCHESTRATOR_TEST_JOURNAL_CLAIM_BARRIER;
    if (!barrier) {
      return;
    }
    mkdirSync(barrier, { recursive: true });
    writeFileSync(join(barrier, `${process.pid}.ready`), "");
    const release = join(barrier, "release");
    const deadline = Date.now() + 10_000;
    while (!existsSync(release)) {
      if (Date.now() >= deadline) {
        throw new Error("installer_test_barrier_timeout");
      }
      Atomics.wait(WAIT_ARRAY, 0, 0, 10);
    }
  },

  durableMutation({ label, path }) {
    durableMutationCount += 1;
    if (
      requestedMutation(
        "HERDR_ORCHESTRATOR_TEST_REWRITE_AFTER_MUTATION",
      ) === durableMutationCount
      || label.startsWith(
        process.env.HERDR_ORCHESTRATOR_TEST_REWRITE_AT_LABEL_PREFIX
          ?? "\u0000",
      )
    ) {
      if (path === null) {
        throw new Error(`installer_test_rewrite_unavailable: ${label}`);
      }
      writeFileSync(
        path,
        Buffer.from("installer test user edit after durable mutation\n"),
      );
    }
    if (
      requestedMutation(
        "HERDR_ORCHESTRATOR_TEST_INTERRUPT_AFTER_MUTATION",
      ) === durableMutationCount
      || label === process.env.HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL
      || label.startsWith(
        process.env.HERDR_ORCHESTRATOR_TEST_INTERRUPT_AT_LABEL_PREFIX
          ?? "\u0000",
      )
    ) {
      process.stderr.write(`installer_test_interruption: ${label}\n`);
      process.exit(86);
    }
  },
};
