const jsdom = require("jsdom");
const { JSDOM } = jsdom;
const fs = require("fs");

const virtualConsole = new jsdom.VirtualConsole();
virtualConsole.on("error", (...args) => { console.error("Error:", ...args); });
virtualConsole.on("jsdomError", (e) => { console.error("JSDOM Error:", e.message); });

const html = fs.readFileSync(0, "utf8");
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  virtualConsole,
  url: "http://127.0.0.1:8333/",
  beforeParse(window) {
    window.fetch = async (url) => {
      console.log("Mock fetch:", url);
      return { json: async () => ({ ok: true, instance: null }) };
    };
  }
});
setTimeout(() => {
  console.log("DOM updated values:");
  console.log("nodeName:", dom.window.document.getElementById("nodeName").textContent);
}, 500);
