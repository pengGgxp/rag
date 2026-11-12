import assert from "node:assert/strict";
import test from "node:test";
import { draftImageConfig, emptyImageConfig } from "../app/model-types.ts";

test("legacy empty image configuration produces a complete disabled draft", () => {
  for (const value of [undefined, null, {}]) {
    const draft = draftImageConfig(value);
    assert.deepEqual(draft, emptyImageConfig);
    assert.equal(draft.specs.length, 0);
    assert.equal(draft.generate, false);
    assert.equal(draft.edit, false);
  }
});

test("partial configuration keeps its values and defaults only missing fields", () => {
  const config = { protocol: "gemini", max_references: 4 };
  assert.deepEqual(draftImageConfig(config), { ...emptyImageConfig, ...config });
  assert.deepEqual(config, { protocol: "gemini", max_references: 4 });
});

test("existing specs, defaults and prices remain intact and drafts do not mutate their source", () => {
  const config = { ...emptyImageConfig, generate: true, preset_id: "gpt-image-1", preset_revision: 1,
    defaults: { generate: { size: "1024x1024", quality: "high" } },
    specs: [{ operation: "generate", size: "1024x1024", quality: "high", credits_per_image: 12.3456, cost_per_image: 0 }] };
  const draft = draftImageConfig(config);
  assert.deepEqual(draft, config);
  draft.specs[0].credits_per_image = 0;
  draft.defaults.generate.quality = "low";
  assert.equal(config.specs[0].credits_per_image, 12.3456);
  assert.equal(config.defaults.generate.quality, "high");
  draftImageConfig().specs.push(config.specs[0]);
  assert.deepEqual(emptyImageConfig.specs, []);
});
