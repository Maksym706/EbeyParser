// «Сборки» (docs/design/PROJECTS.md) — one route "/projects/*", dispatched by params.rest:
//   ""                 → the list (§3)
//   "new"              → creation (§4), ?template=llm_48 preselects a template
//   ":id"              → the build: plan view while draft (§5), tracking view otherwise (§6)
//   ":id/slot/:slot"   → one part full-screen (phone)
import { html } from "../../lib/html.js";
import { EmptyState, Button } from "../../ui/index.js";
import ProjectsList from "./list.js";
import CreateProject from "./create.js";
import ProjectPage from "./project.js";

export default function ProjectsScreen({ params = {}, query = {} }) {
  const rest = (params.rest || "").split("/").filter(Boolean);
  if (!rest.length) return html`<${ProjectsList} query=${query} />`;
  if (rest[0] === "new" && rest.length === 1) return html`<${CreateProject} query=${query} />`;
  if (/^\d+$/.test(rest[0]) && (rest.length === 1 || (rest[1] === "slot" && rest[2] && rest.length === 3)))
    return html`<${ProjectPage} key=${rest[0]} id=${rest[0]} slotKey=${rest[2] || null} query=${query} />`;
  return html`<${EmptyState}
    icon="compass"
    title="Такой страницы нет"
    message="Возможно, ссылка устарела."
    action=${html`<${Button} variant="primary" icon="boxes" href="/projects">К сборкам<//>`}
  />`;
}
