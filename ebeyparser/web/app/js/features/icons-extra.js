// Extra Lucide icons (ISC, lucide-static 1.48.0) used by the Frontend-B screens and not yet
// in ui/icons.js. Registered into the shared ICONS map on import (additive, never overrides).
import { ICONS } from "../ui/icons.js";

const EXTRA = {
};

for (const [name, body] of Object.entries(EXTRA)) if (!(name in ICONS)) ICONS[name] = body;
