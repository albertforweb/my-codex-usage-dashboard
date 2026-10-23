import { App } from '@modelcontextprotocol/ext-apps';

const app = new App({ name: 'My Usage', version: '0.2.5' });
let hasData = false;
function receive(result) {
  let data = result?.structuredContent;
  if (!data && result?.content) {
    const text = result.content.find(item => item.type === 'text')?.text;
    if (text) { try { data = JSON.parse(text); } catch { /* handled below */ } }
  }
  if (data) {
    hasData = true;
    window.dispatchEvent(new CustomEvent('my-usage-data', { detail: data }));
  } else if (result?.isError) {
    const text = result.content?.find(item => item.type === 'text')?.text || 'Usage scan failed.';
    window.dispatchEvent(new CustomEvent('my-usage-error', { detail: text }));
  }
}
app.ontoolresult = receive;
window.myUsageRefresh = async () => {
  try {
    const scan = await app.callServerTool({ name: 'usage.scan', arguments: {} });
    if (scan?.isError) return receive(scan);
    receive(await app.callServerTool({ name: 'usage.dashboard', arguments: {} }));
  }
  catch (error) { window.dispatchEvent(new CustomEvent('my-usage-error', { detail: error.message || String(error) })); }
};
app.connect().then(() => {
  // A tool result normally arrives with the global entrypoint. Fall back for hosts
  // that open the resource without an initial tool call.
  setTimeout(async () => {
    if (!hasData) {
      try { receive(await app.callServerTool({ name: 'usage.dashboard', arguments: {} })); }
      catch (error) { window.dispatchEvent(new CustomEvent('my-usage-error', { detail: error.message || String(error) })); }
    }
  }, 1200);
}).catch(error => window.dispatchEvent(new CustomEvent('my-usage-error', { detail: error.message || String(error) })));
