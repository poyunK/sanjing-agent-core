// 可选开发检查：仅验证新生成的本地看板，不控制用户浏览器。
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function main() {
  const {chromium} = require(path.join(process.argv[2], 'playwright'));
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, '.handoff-local', 'verification');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({executablePath:process.argv[3], headless:true});
  const results = [];
  try {
    const page = await browser.newPage();
    await page.route(/^https?:/, route => route.abort());
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height:900});
      await page.goto(pathToFileURL(path.join(root, '.handoff-local', 'BOARD.html')).href);
      const text = await page.locator('body').innerText();
      results.push({width,
        title: await page.title(),
        columns: await page.locator('.column').count(),
        chinese_labels: ['待办','已派单','执行中','待验收','需返工'].every(label => text.includes(label)),
        no_scripts: await page.locator('script').count() === 0,
        document_fits: await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        csp: await page.locator('meta[http-equiv="Content-Security-Policy"]').count() === 1,
      });
      await page.screenshot({path:path.join(output, `board-${width}.png`), fullPage:true});
    }
    const passed = results.every(item => item.columns === 5 && item.chinese_labels && item.no_scripts && item.document_fits && item.csp);
    fs.writeFileSync(path.join(output,'render-report.json'), JSON.stringify({passed,results},null,2),'utf8');
    console.log(JSON.stringify({passed,results}));
    if (!passed) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}
main().catch(() => { console.error('看板本地渲染检查未完成'); process.exitCode = 1; });
