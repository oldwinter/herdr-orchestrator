#!/usr/bin/env node

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  readdirSync,
  readFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { TextDecoder } from "node:util";
import { fileURLToPath } from "node:url";

import {
  installManagerLight,
  managerLightStatus,
  uninstallManagerLight,
} from "../plugins/manager-light/configure.mjs";
import {
  METADATA_SOURCE,
  tokenPatchFor,
} from "../plugins/manager-light/projection.mjs";
import {
  INSTALLER_HARNESSES,
  installerFileState,
  inspectInstallerJournal,
  observeInstallerTarget,
  reconcileInstallerJournal,
  runInstallerTransaction,
} from "./installer-journal.mjs";
