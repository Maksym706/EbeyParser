// One import for every screen: `html` tagged templates (htm bound to Preact) + hooks.
//   import { html, useState, useEffect } from "../lib/html.js";
import { h, render, Fragment, createContext, cloneElement, createRef, toChildArray } from "preact";
import htm from "htm";
export {
  useState,
  useEffect,
  useLayoutEffect,
  useMemo,
  useCallback,
  useRef,
  useReducer,
  useContext,
  useId,
  useErrorBoundary,
} from "preact/hooks";

export const html = htm.bind(h);
export { h, render, Fragment, createContext, cloneElement, createRef, toChildArray };

/** Join class names: cx("btn", primary && "btn--primary", { "is-on": on }). */
export function cx(...parts) {
  const out = [];
  for (const p of parts) {
    if (!p) continue;
    if (typeof p === "string") out.push(p);
    else if (typeof p === "object") for (const k in p) if (p[k]) out.push(k);
  }
  return out.join(" ");
}
