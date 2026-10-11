// 只打开本地账本，阻断外网；运行内核本身无需这些开发依赖。
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function main() {
  const {chromium} = require(path.join(process.argv[2], 'playwright'));
  const output = path.resolve(__dirname, '../runtime/search_verification');
  const live = JSON.parse(fs.readFileSync(path.join(output, 'report.json'), 'utf8'));
  const browser = await chromium.launch({executablePath:process.argv[3], headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:900}});
    await page.route(/^https?:/, route => route.abort());
    await page.goto(pathToFileURL(live.ledger_path).href);
    const body = await page.locator('body').innerText();
    const result = {
      chinese:body.includes('豆包网页搜索来源记录'),
      summary_scope:body.includes('未阅读全文'),
      sources:await page.locator('[data-kind="professional-record"]').count() > 0,
      no_scripts:await page.locator('script').count() === 0,
      desktop_fits:await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    };
    await page.locator('details').last().locator('summary').click();
    result.separate_usage = (await page.locator('body').innerText()).includes('豆包搜索使用统计');
    result.events_expand = await page.locator('details').last().locator('tbody tr').count() > 0;
    await page.screenshot({path:path.join(output, 'ledger-preview.png')});
    await page.setViewportSize({width:390,height:844});
    result.mobile_fits = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
    result.passed = Object.values(result).every(value => value === true);
    fs.writeFileSync(path.join(output, 'render-report.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
    if (!result.passed) process.exitCode = 1;
  } finally {await browser.close();}
}
main().catch(() => {console.error('本地搜索账本渲染检查失败');process.exitCode=1;});
