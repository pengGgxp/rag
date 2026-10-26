// Run locally through playwright-cli run-code --filename after opening the upload dialog.
// Prepare output/upload-200mb.txt (200 MiB) and upload-too-large.txt (200 MiB + 1).
async (page) => {
  const dialog = page.getByRole('dialog');
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  assert(await dialog.textContent().then(text => text.includes('200 MB') && !text.includes('每份最多')), 'Upload limits copy is stale');
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (String(args[0]).includes('/documents') && args[1]?.method === 'POST')
        await new Promise(resolve => setTimeout(resolve, 16000));
      return response;
    };
  });
  await dialog.locator('input[type=file]').setInputFiles('output/upload-200mb.txt');
  const responsePromise = page.waitForResponse(response => response.url().includes('/documents') && response.request().method() === 'POST', { timeout: 120000 });
  await dialog.getByRole('button', { name: '上传文档', exact: true }).click();
  const response = await responsePromise;
  assert(response.status() === 201, 'Real upload failed: ' + response.status() + ' ' + await response.text());
  const uploaded = await response.json();
  assert(uploaded.size_bytes === 200 * 1024 * 1024, 'Stored document size differs');
  const session = await page.request.get('http://localhost:8000/auth/me', { headers: { 'X-RAG-Request': '1' } });
  const headers = { 'X-RAG-Request': '1', 'X-CSRF-Token': (await session.json()).csrf_token };
  const cancelled = await page.request.post('http://localhost:8000/jobs/' + uploaded.job.id + '/cancel', { headers });
  assert(cancelled.ok(), 'Disposable ingestion task was not cancelled');
  await dialog.waitFor({ state: 'detached', timeout: 120000 });
  assert(uploaded, 'No actual API upload observed');
  await page.getByRole('button', { name: '上传文档', exact: true }).first().click();
  await dialog.locator('input[type=file]').setInputFiles('output/upload-too-large.txt');
  await dialog.getByText('upload-too-large.txt 超过 200 MB', { exact: true }).waitFor();
  assert(await dialog.getByRole('button', { name: '上传文档', exact: true }).isDisabled(), 'Oversized upload remained enabled');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Upload layout overflows');
    await page.screenshot({ path: 'output/playwright/upload-' + width + '.png' });
  }
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'detached' });
  console.log('Real 200 MiB storage upload, delayed response, oversized rejection and desktop/mobile dialog passed.');
}
