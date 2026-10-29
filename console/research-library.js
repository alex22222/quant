/* 跨市场知识条目独立加载，不参与 A 股策略注册与执行。 */
(() => {
  const library = document.getElementById('okx-research-library');
  const summary = document.getElementById('okx-research-summary');
  if (!library || !summary) return;

  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const labels = {
    profit_source:'收益来源', risks:'主要风险', evidence_boundary:'证据边界',
    parameters_required:'仍需核实的参数', validation:'验证方法', assessment:'研究判断'
  };

  async function load() {
    summary.textContent = '正在读取研究目录…';
    library.replaceChildren();
    try {
      const response = await fetch('/strategies/research/okx_catalog.json', {cache:'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const catalog = await response.json();
      if (catalog.schema_version !== 1 || !Array.isArray(catalog.strategies)
          || !catalog.strategies.length || !catalog.sources
          || catalog.strategies.some(row => row.execution_enabled !== false
            || row.state !== 'research' || row.implementation_status !== 'not_implemented'
            || row.verified_metrics !== null || !Array.isArray(row.variants))) {
        throw new Error('研究目录格式或隔离标记不符');
      }
      const cards = catalog.strategies.map(row => {
        const card = element('article', null, 'card');
        card.style.minWidth = '0';
        card.style.overflowWrap = 'anywhere';
        const head = element('div', null, 'lib-head');
        head.append(element('h3', row.name, 'lib-name'), element('span', '仅研究 · 未实现', 'pill research'));
        card.append(head, element('p', row.variants.join(' · '), 'sub'), element('p', row.principle));
        const details = element('details');
        details.append(element('summary', '收益、风险、参数与来源'));
        for (const [key, label] of Object.entries(labels)) {
          const paragraph = element('p');
          paragraph.append(element('b', label + '：'), document.createTextNode(row[key]));
          details.append(paragraph);
        }
        const sources = element('p', '官方机制参考：', 'sub');
        for (const id of row.sources) {
          const source = catalog.sources[id];
          const url = new URL(source.url);
          if (url.protocol !== 'https:' || url.hostname !== 'www.okx.com') {
            throw new Error('来源地址不符');
          }
          const link = element('a', source.title, 'overview-link');
          link.href = url.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          sources.append(document.createTextNode(' · '), link);
        }
        details.append(sources, element('p', '截图 ' + row.screenshot + ' · 参数未取得 · 无已验证业绩', 'sub'));
        card.append(details);
        return card;
      });
      const count = catalog.strategies.reduce((total, row) => total + row.variants.length, 0);
      summary.textContent = `${cards.length} 类 · ${count} 个截图模板 · 核对 ${catalog.reviewed_at}。${catalog.notice}`;
      library.replaceChildren(...cards);
    } catch (error) {
      library.replaceChildren();
      summary.textContent = '研究目录读取失败，不能视为空库：' + error.message + ' ';
      const retry = element('button', '重试', 'overview-link');
      retry.addEventListener('click', load);
      summary.append(retry);
    }
  }
  load();
})();
