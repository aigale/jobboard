// Point the app at your live site:  npm run set-url -- https://your-domain.ph
const fs = require("fs");
const path = require("path");
const url = (process.argv[2] || "").replace(/\/+$/, "");
if (!/^https:\/\/[^/]+$/.test(url)) {
  console.error("Give your site's address, starting with https://, e.g.  npm run set-url -- https://sinagjobs.ph");
  process.exit(1);
}
const file = path.join(__dirname, "..", "capacitor.config.json");
const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
cfg.server.url = url;
fs.writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n");
const www = path.join(__dirname, "..", "www", "index.html");
fs.writeFileSync(www, fs.readFileSync(www, "utf8").replace(/https:\/\/[^"']+(?=["'] \/\/ SITE)/, url));
console.log("The app now opens " + url + ". Next: npx cap sync");
