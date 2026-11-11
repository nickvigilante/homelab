// Global (self-hosted) Renovate config. Per-repo behavior lives in each repo's
// own renovate.json.
const fs = require("fs");

module.exports = {
  platform: "github",
  // Written by the get-token init container; installation tokens last an hour.
  token: fs.readFileSync("/token/token", "utf8").trim(),
  repositories: [
    "nickvigilante/homelab",
  ],
  // Each repo opts in by committing a renovate.json. Without one, Renovate skips
  // the repo instead of opening an onboarding PR.
  onboarding: false,
  requireConfig: "required",
};
