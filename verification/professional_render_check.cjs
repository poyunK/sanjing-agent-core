// 可选的交付检查；Agent 运行本身不需要 Node、Playwright 或 Edge。
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

async function main() {
  const {chromium} = require(path.join(process.argv[2], 'playwright'));
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'runtime', 'professional_verification');
  const replay = JSON.parse(fs.readFileSync(path.join(output, 'replay_report.json'), 'utf8'));
  const latest = fs.readdirSync(path.join(root, 'runtime', 'ledger'))
    .filter(name => name.endsWith('.html'))
    .map(name => ({name, time: fs.statSync(path.join(root, 'runtime', 'ledger', name)).mtimeMs}))
    .sort((a, b) => b.time - a.time)[0];
  const cases = [{case:'offline', ledger_path:path.join(root, 'runtime', 'ledger', latest.name)}, ...replay.cases];
  const browser = await chromium.launch({executablePath:process.argv[3], headless:true});
  const results = [];
  try {
    const page = await browser.newPage({viewport:{width:1100, height:900}});
    // 页面检查只读取本地文件。远程来源链接保留可点击，但本脚本不访问它们。
    await page.route(/^https?:/, route => route.abort());
    for (const item of cases) {
      await page.goto(pathToFileURL(item.ledger_path).href);
      const text = await page.locator('body').innerText();
      const result = {case:item.case, title:await page.title(),
        decisions:await page.locator('[data-kind="career-decision"]').count(),
        no_scripts:await page.locator('script').count() === 0,
        no_horizontal_overflow:await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        chinese_labels:text.includes('采用的依据') && text.includes('来源字段事实'),
        source_records:await page.locator('[data-kind="professional-record"]').count()};
      if (item.case === 'offline') {
        await page.screenshot({path:path.join(output,'ledger-preview.png')});
      }
      if (item.case === 'industry') {
        result.has_month = text.includes('当月同比');
        result.has_period = text.includes('统计期（原字段）');
        result.has_unit = text.includes('单位（原字段）');
        await page.screenshot({path:path.join(output,'industry-preview.png')});
      }
      const detail = page.locator('details').last();
      await detail.locator('summary').click();
      result.events_expand = await detail.locator('tbody tr').count() > 0;
      result.has_usage = (await page.locator('body').innerText()).includes('本轮使用统计')
        || item.case !== 'offline';
      await page.setViewportSize({width:390,height:844});
      result.mobile_no_overflow = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
      await page.setViewportSize({width:1100,height:900});
      results.push(result);
    }
    const passed = results.every(row => row.decisions > 0 && row.no_scripts && row.no_horizontal_overflow
      && row.chinese_labels && row.source_records > 0 && row.events_expand && row.mobile_no_overflow && row.has_usage)
      && results.find(row => row.case === 'industry').has_period;
    fs.writeFileSync(path.join(output,'render_report.json'), JSON.stringify({passed, cases:results}, null, 2), 'utf8');
    console.log(JSON.stringify({passed, cases:results}));
    if (!passed) process.exitCode = 1;
  } finally { await browser.close(); }
}
main().catch(() => {console.error('本地页面检查失败；请核对生成的快照与浏览器环境。'); process.exitCode = 1;});
