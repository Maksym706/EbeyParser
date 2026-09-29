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
  useErrorBoundary,
} from "preact/hooks";
import { useState as useStateHook } from "preact/hooks";

let uid = 0;
/**
 * Stable id, unique across ALL render roots. Preact's own useId restarts in every root, and our
 * Portals (drawers, dialogs) are separate roots — so <label for> could point at a control on the
 * page underneath (P2-24).
 */
export function useId() {
  const [id] = useStateHook(() => `id-${(++uid).toString(36)}`);
  return id;
}

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
