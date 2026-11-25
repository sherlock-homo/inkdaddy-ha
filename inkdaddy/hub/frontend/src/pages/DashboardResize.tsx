import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import type { DashboardWidget } from "../lib/types";
import { fits, widgetSpans } from "./DashboardWidgets";

type Edge = "left" | "right" | "top" | "bottom";
const edges: Edge[] = ["left", "right", "top", "bottom"];
const horizontal = (edge: Edge) => edge === "left" || edge === "right";
const direction = (edge: Edge) => edge === "left" || edge === "top" ? -1 : 1;
const length = (widget: DashboardWidget, edge: Edge) => horizontal(edge) ? widget.w : widget.h;

function choicesForEdge(widget: DashboardWidget, widgets: DashboardWidget[], edge: Edge): DashboardWidget[] {
  return widgetSpans(widget)
    .filter(([w, h]) => horizontal(edge) ? h === widget.h : w === widget.w)
    .map(([w, h]) => ({
      ...widget, w, h,
      x: edge === "left" ? widget.x + widget.w - w : widget.x,
      y: edge === "top" ? widget.y + widget.h - h : widget.y,
    }))
    .filter((rect) => fits(widgets, rect, rect.w, rect.h, widget))
    .sort((a, b) => length(a, edge) - length(b, edge));
}

interface Drag {
  pointer: number;
  edge: Edge;
  origin: DashboardWidget;
  options: DashboardWidget[];
  start: number;
  step: number;
}

export function ResizeHandles({ widget, widgets, disabled, onResize }: {
  widget: DashboardWidget; widgets: DashboardWidget[]; disabled: boolean; onResize: (next: DashboardWidget) => void;
}) {
  const drag = useRef<Drag | null>(null);

  function start(event: PointerEvent<HTMLButtonElement>, edge: Edge, options: DashboardWidget[]) {
    if (event.button !== 0 || drag.current || options.length < 2) return;
    const grid = event.currentTarget.closest(".dd-canvas");
    if (!grid) return;
    const style = getComputedStyle(grid);
    const step = horizontal(edge)
      ? parseFloat(style.gridTemplateColumns) + parseFloat(style.columnGap)
      : parseFloat(style.gridTemplateRows) + parseFloat(style.rowGap);
    if (!(step > 0)) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointer: event.pointerId, edge, origin: widget, options, step,
      start: horizontal(edge) ? event.clientX : event.clientY };
  }

  function move(event: PointerEvent<HTMLButtonElement>) {
    const active = drag.current;
    if (!active || active.pointer !== event.pointerId) return;
    const { edge, origin, options, start, step } = active;
    const delta = (horizontal(edge) ? event.clientX : event.clientY) - start;
    const target = length(origin, edge) + direction(edge) * delta / step;
    const next = options.reduce((closest, candidate) =>
      Math.abs(length(candidate, edge) - target) < Math.abs(length(closest, edge) - target) ? candidate : closest, origin);
    if (length(next, edge) !== length(widget, edge)) onResize(next);
  }

  function keyboard(event: KeyboardEvent<HTMLButtonElement>, edge: Edge, options: DashboardWidget[]) {
    const keys = horizontal(edge) ? ["ArrowLeft", "ArrowRight"] : ["ArrowUp", "ArrowDown"];
    const index = options.findIndex((option) => length(option, edge) === length(widget, edge));
    let nextIndex;
    if (keys.includes(event.key)) nextIndex = index + direction(edge) * (event.key === keys[0] ? -1 : 1);
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = options.length - 1;
    else return;
    event.preventDefault();
    if (options[nextIndex]) onResize(options[nextIndex]);
  }

  return edges.map((edge) => {
    const options = choicesForEdge(widget, widgets, edge);
    if (options.length < 2) return null;
    return <button key={edge} type="button" role="slider" className="dd-resize" data-edge={edge}
      aria-label={`Resize ${edge} edge`} aria-orientation={horizontal(edge) ? "horizontal" : "vertical"}
      aria-valuemin={length(options[0], edge)} aria-valuemax={length(options[options.length - 1], edge)}
      aria-valuenow={length(widget, edge)} aria-valuetext={`${widget.w} columns by ${widget.h} rows`}
      disabled={disabled} onKeyDown={(event) => keyboard(event, edge, options)}
      onPointerDown={(event) => start(event, edge, options)} onPointerMove={move}
      onPointerUp={(event) => { if (drag.current?.pointer === event.pointerId) { move(event); drag.current = null; } }}
      onPointerCancel={(event) => {
        if (drag.current?.pointer === event.pointerId) { onResize(drag.current.origin); drag.current = null; }
      }} onLostPointerCapture={(event) => { if (drag.current?.pointer === event.pointerId) drag.current = null; }} />;
  });
}
