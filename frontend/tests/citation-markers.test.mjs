import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import { citationMarkers } from "../app/citation-markers.ts";

const id = "76c20d93-5f9a-41c5-998e-9962bd76cd53";
const other = "second-chunk";
const render = (text, citations = [{ chunk_id: id }, { chunk_id: other }]) =>
  renderToStaticMarkup(React.createElement(ReactMarkdown,
    { remarkPlugins: [citationMarkers(citations)], children: text }));
const links = text => (text.match(/href="#source-/g) ?? []).length;

test("renders both actual IDs and prefixed markers with stable source numbers", () => {
  const html = render(`[${id}] [chunk_id: ${id}] [ chunk_id： ${other} ] [CHUNK_ID : ${id}]`);
  assert.equal(links(html), 4);
  assert.equal((html.match(new RegExp(`#source-${id}`, "g")) ?? []).length, 3);
  assert.ok(html.includes(`href="#source-${other}">2</a>`));
  assert.ok(!html.includes("chunk_id:"));
});

test("only links authorized sources, including when sources arrive after text", () => {
  const text = `[chunk_id: ${id}] [chunk_id: unknown]`;
  assert.equal(links(render(text, [])), 0);
  assert.equal(links(render(text)), 1);
  assert.ok(render(text).includes("[chunk_id: unknown]"));
});

test("preserves inline code, fenced code, images and existing links", () => {
  const marker = `[chunk_id: ${id}]`;
  const html = render(`\`${marker}\`\n\n\`\`\`text\n${marker}\n\`\`\`\n\n` +
    `[${marker}](https://example.com)\n\n![${marker}](https://example.com/image.png)\n\n${marker}`);
  assert.equal(links(html), 1);
  assert.ok(html.includes(`<code>${marker}</code>`));
  assert.ok(html.includes('<a href="https://example.com">'));
  assert.ok(html.includes('alt="[chunk_id:'));
});

test("does not create a source link for an incomplete streamed marker", () => {
  const prefix = `[chunk_id: ${id}`;
  assert.equal(links(render(prefix)), 0);
  assert.equal(links(render(`${prefix}]`)), 1);
  assert.equal(links(render(`[chunk_id: ${id}]\n\n> [${id}]`)), 2);
});
