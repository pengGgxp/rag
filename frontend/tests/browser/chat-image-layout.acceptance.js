// Run from the repository root with playwright-cli run-code --filename frontend/tests/browser/chat-image-layout.acceptance.js after a local production build.
// All API and image traffic is intercepted; no production data, account or model calls.
async (page) => {
  const base = "http://localhost:3127", api = "http://localhost:8127";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const build = "frontend/.next";
  const name = "viewport-test.png", fileId = "layout-image";
  const stamp = "2026-11-20T10:00:00Z";
  const messages = Array.from({ length: 38 }, (_, sequence) => ({
    id: `m${sequence}`, role: sequence % 2 ? "assistant" : "user", status: "completed", sequence,
    content: `Layout fixture message ${sequence}`, citations: [], traces: [], refused: false,
  }));
  messages[1].tool_events = [{ call_id: "write-image", name: "write_file", label: "Image", status: "completed",
    files: [{ id: fileId, path: name, version: 1, size_bytes: 2048 }] }];
  messages[1].display_blocks = [{ id: "activity", type: "activity", call_id: "write-image" },
    { id: "answer", type: "text", text: messages[1].content }];
  const conversation = { id: "fixture", title: "Image layout", title_source: "manual", revision: 1,
    updated_at: stamp, active_request_id: null, knowledge_bases: [], compactions: [], skill_ids: [], mcp_server_ids: [], messages };
  const image = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 120; canvas.height = 480;
    const pen = canvas.getContext("2d");
    pen.fillStyle = "#277988"; pen.fillRect(0, 0, 120, 480);
    return canvas.toDataURL("image/png").split(",")[1];
  }), "base64");
  let gate, reads = 0, access = 0, denied = false;
  const hold = () => {
    let release;
    gate = new Promise(resolve => { release = resolve; });
    return release;
  };
  await page.route(`${base}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") return route.fulfill({ path: `${build}/server/app/index.html`, contentType: "text/html" });
    if (path === "/icon.svg") return route.fulfill({ path: "frontend/app/icon.svg", contentType: "image/svg+xml" });
    if (path.startsWith("/_next/static/"))
      return route.fulfill({ path: `${build}/${path.replace("/_next/", "")}`,
        contentType: path.endsWith(".js") ? "application/javascript" : path.endsWith(".css") ? "text/css" : undefined });
    return route.fulfill({ status: 404 });
  });
  await page.route(`${api}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    const headers = { "access-control-allow-origin": base, "access-control-allow-credentials": "true",
      "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-expose-headers": "etag", etag: "image-fixture" };
    const send = json => route.fulfill({ json, headers });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (path === "/auth/me") return send({ user: { id: "layout-user", name: "Layout", email: "layout@example.invalid" },
      workspace_id: "layout-workspace", workspaces: [{ id: "layout-workspace", name: "Layout", role: "editor" }], csrf_token: "fixture" });
    if (path === "/auth/options") return send({ first_run: false });
    if (path === "/conversations") return send({ items: [conversation], next_cursor: null });
    if (path === "/conversations/fixture") return send(conversation);
    if (path === "/knowledge-bases") return send([]);
    if (path === "/models/available") return send({ providers: [] });
    if (path === `/resources/${fileId}/preview-access`) {
      access++;
      if (denied) return route.fulfill({ status: 403, json: { detail: "denied" }, headers });
      return send({ kind: "image", revision: "layout-1", etag: "image-fixture", size_bytes: image.length,
        headers: {}, expires_at: Date.now() / 1000 + 600, url: `${api}/image-bytes` });
    }
    if (path === "/image-bytes") {
      reads++;
      await gate;
      return route.fulfill({ body: image, contentType: "image/png", headers });
    }
    return send({});
  });
  const area = page.getByRole("region", { name: "对话内容" });
  const frame = page.locator(".message-file-image > .inline-file-image");
  const img = frame.locator("img");
  const loading = frame.getByRole("status");
  const fileLink = page.getByRole("link", { name: `查看文件：${name}`, exact: true });
  const scroll = edge => area.evaluate((node, edge) => node.scrollTop = edge === "start" ? 0 : node.scrollHeight, edge);
  const layout = () => frame.evaluate(node => {
    const link = node.nextElementSibling.getBoundingClientRect(), box = node.getBoundingClientRect();
    return [box.width, box.height, link.top - box.top, node.closest(".chat-messages").scrollHeight];
  });
  const same = async (expected, stage) => {
    const actual = await layout();
    assert(expected.every((value, index) => Math.abs(actual[index] - value) < 1), `${stage}: ${expected} changed to ${actual}`);
  };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/?conversation=fixture`);
  await frame.waitFor();
  await scroll("end");
  await loading.waitFor();
  assert(reads === 0 && access === 0, "Far-offscreen image fetched at page load");
  const release = hold();
  await scroll("start");
  await loading.waitFor();
  const firstLayout = await layout();
  release();
  await img.evaluate(node => node.decode());
  assert(await img.evaluate(node => node.naturalHeight === 480 && getComputedStyle(node).objectFit === "contain"), "Portrait image not loaded");
  await same(firstLayout, "desktop first load");
  await fileLink.waitFor();
  await page.screenshot({ path: "output/playwright/chat-image-layout-desktop.png" });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const expected = await layout();
    await scroll("end");
    await img.waitFor({ state: "detached" });
    await same(expected, `${width}px after scrolling away`);
    await scroll("start");
    await img.evaluate(node => node.decode());
    await same(expected, `${width}px after scrolling back`);
  }
  await page.screenshot({ path: "output/playwright/chat-image-layout-mobile.png" });
  await scroll("end");
  await img.waitFor({ state: "detached" });
  denied = true;
  await scroll("start");
  await frame.getByRole("button", { name: "重试" }).waitFor();
  assert(await img.count() === 0 && access >= 4 && reads === 1, "Return should reauthorize, reuse cached bytes, and hide revoked images");
  return "PASS: lazy load, stable first load and scroll return (desktop and mobile), cache reuse and permission revocation";
}
