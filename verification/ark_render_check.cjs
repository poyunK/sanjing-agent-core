// 验收本次真实与离线账本；只访问本地文件，不请求来源网站。
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function main() {
  const {chromium} = require(path.join(process.argv[2], 'playwright'));
  const directory = path.resolve(__dirname, '../runtime/ark_verification');
  const live = JSON.parse(fs.readFileSync(path.join(directory, 'report.json'), 'utf8'));
  const pages = fs.readdirSync(path.join(directory, 'ledger')).filter(name => name.endsWith('.html'));
  const cases = live.cases.map(row => ({case: row.case, ledger_path: row.ledger_path}));
  const livePaths = new Set(cases.map(row => row.ledger_path));
  for (const name of pages) {
    const pagePath = path.join(directory, 'ledger', name);
    if (!livePaths.has(pagePath)) cases.push({case:'offline', ledger_path:pagePath});
  }
  const browser = await chromium.launch({executablePath:process.argv[3], headless:true});
  const rows = [];
  try {
    const page = await browser.newPage({viewport:{width:1100, height:900}});
    await page.route(/^https?:/, route => route.abort());
    for (const item of cases) {
      await page.goto(pathToFileURL(item.ledger_path).href);
      const text = await page.locator('body').innerText();
      const row = {case:item.case,
        chinese:(await page.locator('h1').innerText()).includes('账本') && text.includes('执行'),
        no_scripts:await page.locator('script').count() === 0,
        no_private_continuation:!text.includes('_responses_output') && !text.includes('encrypted_content'),
        desktop_fits:await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        quality_flags:item.case === 'demo' || text.includes('适用性提示'),
        source_records:item.case === 'demo' || await page.locator('[data-kind="professional-record"]').count() > 0};
      const detail = page.locator('details').last();
      await detail.locator('summary').click();
      row.expand_events = await detail.locator('tbody tr').count() > 0;
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({path:path.join(directory,item.case+'-preview.png')});
      await page.setViewportSize({width:390,height:844});
      row.mobile_fits = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
      await page.setViewportSize({width:1100,height:900});
      rows.push(row);
    }
    const passed = rows.every(row => Object.entries(row).every(([key,value]) => key === 'case' || value === true));
    fs.writeFileSync(path.join(directory,'render-report.json'),JSON.stringify({passed,cases:rows},null,2));
    console.log(JSON.stringify({passed,cases:rows}));
    if (!passed) process.exitCode = 1;
  } finally {await browser.close();}
}
main().catch(() => {console.error('本地账本渲染检查失败'); process.exitCode=1;});
