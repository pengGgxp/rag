import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { MAX_SKILL_BYTES, skillUploadError } from "../app/extensions/skill-upload.ts";

test("Skill uploads accept 200 MiB and reject one extra byte", () => {
  assert.equal(MAX_SKILL_BYTES, 200 * 1024 * 1024);
  assert.equal(skillUploadError([{ size: 26 * 1024 * 1024 }]), null);
  assert.equal(skillUploadError([{ size: MAX_SKILL_BYTES }]), null);
  assert.equal(skillUploadError([{ size: MAX_SKILL_BYTES + 1 }]), "Skill 包不能超过 200 MiB");
});

test("folder uploads check individual files and total bytes without a file-count cap", () => {
  assert.equal(skillUploadError([{ size: MAX_SKILL_BYTES - 1 }, { size: 1 }], true), null);
  assert.equal(skillUploadError([{ size: MAX_SKILL_BYTES }, { size: 1 }], true), "Skill 总大小不能超过 200 MiB");
  assert.equal(skillUploadError([{ size: MAX_SKILL_BYTES + 1 }], true), "单个 Skill 文件不能超过 200 MiB");
  assert.equal(skillUploadError(Array.from({ length: 1000 }, () => ({ size: 1 })), true), null);
});

test("main and test proxy templates allow encoded 200 MiB Skill saves", () => {
  const requestBytes = 4 * Math.ceil(MAX_SKILL_BYTES / 3) + 1024 * 1024;
  for (const path of ["deploy/nginx/zk.str1ct.top.conf", "scripts/nginx-zhiku-auth.conf", "deploy/testing/nginx.conf"]) {
    const config = readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
    const limits = [...config.matchAll(/^\s*client_max_body_size\s+(\d+)m;/gm)];
    assert.ok(limits.length, `missing request limit in ${path}`);
    for (const [, mib] of limits) assert.ok(Number(mib) * 1024 * 1024 >= requestBytes, `encoded Skill blocked by ${path}`);
  }
});
