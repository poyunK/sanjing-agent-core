// 开发时的可选页面检查；运行Agent本身不依赖Node或Playwright。
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function main() {
  const packageRoot = process.argv[2];
  const browserExe = process.argv[3];
  const {chromium} = require(path.join(packageRoot, 'playwright'));
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'runtime', 'verification');
  const live = JSON.parse(fs.readFileSync(path.join(output, 'live_results.json'), 'utf8'));
  const browser = await chromium.launch({executablePath:browserExe, headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:850}});
    await page.goto(pathToFileURL(live.cases[0].ledger_path).href);
    await page.screenshot({path:path.join(output,'ledger-preview.png'), fullPage:false});
    const first = {title:await page.title(), cards:await page.locator('[data-kind="recommendation"]').count(),
      no_scripts:await page.locator('script').count() === 0,
      no_horizontal_overflow:await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)};
    await page.locator('details').last().locator('summary').click();
    first.event_rows = await page.locator('details').last().locator('tbody tr').count();
    await page.goto(pathToFileURL(live.cases[1].ledger_path).href);
    const second = {has_condition:await page.getByText('十五小时',{exact:false}).count()>0,
                    has_question:await page.getByText('需要补充条件',{exact:false}).count()>0};
    const report = {first,second};
    fs.writeFileSync(path.join(output,'render_results.json'),JSON.stringify(report,null,2),'utf8');
    console.log(JSON.stringify(report));
    if(first.cards!==2 || !first.no_scripts || !first.no_horizontal_overflow || !second.has_condition)
      process.exitCode=1;
  } finally { await browser.close(); }
}
main().catch(()=>{console.error('页面验证失败，请检查Node包路径、浏览器路径与已生成的验证记录');process.exitCode=1;});
