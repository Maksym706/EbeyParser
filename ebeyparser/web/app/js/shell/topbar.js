// Screens can put things into the app top bar (desktop) / app bar (phone):
//
//   useTopbar({
//     search: { value, onChange, placeholder: "Найти: 3090…" },   // desktop search field (filters the list)
//     actions: html`<${Button} variant="primary" icon="plus">Новый поиск<//>`,  // primary page action
//     title: "Поиски",                                              // override the route title
//   }, [value]);
//
// Everything is cleared automatically when the screen unmounts.
import { useEffect } from "../lib/html.js";
import { createStore } from "../lib/store.js";

export const topbarStore = createStore({ search: null, actions: null, title: null, owner: 0 });
let seq = 0;

export function useTopbar(config, deps = []) {
  useEffect(() => {
    const owner = ++seq;
    topbarStore.replace({ search: null, actions: null, title: null, ...config, owner });
    return () => {
      if (topbarStore.get().owner === owner) topbarStore.replace({ search: null, actions: null, title: null, owner: 0 });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
