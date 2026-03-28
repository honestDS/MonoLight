# The Design System: LLM Administrative Architecture

## 1. Overview & Creative North Star
**The Creative North Star: "The Clinical Architect"**

This design system moves away from the whimsical "SaaS-pop" aesthetic to embrace an atmosphere of high-utility precision. For an LLM management system, the interface must feel like a high-performance instrument—authoritative, stable, and transparent. We achieve this through "Clinical Minimalism": a strategy that prioritizes high-contrast legibility, rigid alignment, and a sophisticated flat-layered hierarchy.

While the foundation is "flat," we avoid a "thin" look by using intentional surface nesting. The experience should feel like an editorial technical manual—clear, structured, and uncompromisingly professional.

---

## 2. Colors & Surface Logic
The palette is rooted in a neutral gray scale to provide a "lab-like" environment where data is the hero. The primary blue (`#0060a9`) acts as the "Active Signal," used only for critical interactions and indicators.

### Surface Hierarchy & Nesting
To maintain a high-end feel without relying on heavy shadows, we use **Tonal Layering**. 
- **The Base:** All main content areas sit on `surface` (`#faf9fb`).
- **The Sidebar:** To create an authoritative anchor, the sidebar utilizes `inverse_surface` (`#0d0e10`) with `inverse_on_surface` text.
- **The Nesting Principle:** Do not use borders to separate main sections. Instead, use a "Low-to-High" stack:
    1. Base Page: `surface`
    2. Section Containers: `surface_container_low` (`#f3f3f7`)
    3. Interactive Cards/Inputs: `surface_container_lowest` (`#ffffff`)

### The "Subtle Border" Mandate
Per the architecture requirements, we use 1px borders (`outline_variant` at `#afb2b8`) only for structural separation within the light content area. However, to maintain a premium "flat" look, these must never be black. They are "Ghost Borders"—visible enough to define a boundary, but light enough to let the content breathe.

---

## 3. Typography: The Editorial Scale
We utilize **Inter** as our primary typeface. Its tall x-height and neutral grotesque letterforms provide the "Clinical Architect" feel required for complex data.

*   **Display (LLM Performance Metrics):** Use `display-md` (2.75rem) for hero numbers. This scale commands attention and establishes data authority.
*   **Headline (Module Titles):** `headline-sm` (1.5rem) with a `Medium (500)` weight. This creates a clear entry point for administrative modules.
*   **Body (Technical Logs):** Use `body-md` (0.875rem) for standard text. For dense LLM prompt logs, use `body-sm` (0.75rem) to maximize information density.
*   **Labels (Status & Metadata):** `label-md` (0.75rem) in `AllCaps` or `Medium` weight to distinguish metadata from actionable content.

---

## 4. Elevation & Depth
In a flat, high-contrast system, depth is achieved through **High-Resolution Precision** rather than 3D effects.

*   **The Layering Rule:** Use `surface_container_high` (`#e6e8ee`) for hover states on list items. This provides immediate tactile feedback without changing the "flat" nature of the component.
*   **Flat Shadows:** Shadows are strictly prohibited for standard components. The only exception is the "Global Command Bar" or "Floating Modals," which use an ultra-diffused shadow: `0px 4px 20px rgba(13, 14, 16, 0.06)`.
*   **Contrast Density:** High-contrast text (`on_surface`: `#2f3338`) is used against light backgrounds to ensure AAA accessibility—a core requirement for administrative tools.

---

## 5. Components

### Buttons & CTAs
*   **Primary (`primary` #0060a9):** Solid fill, `on_primary` text. Border-radius: `4px` (Token: `DEFAULT`).
*   **Secondary (`secondary_container` #e1e2e7):** For low-priority actions. Flat, no border.
*   **Ghost/Tertiary:** No background, `primary` text. Used for "Cancel" or "Back" actions to reduce visual noise.

### Input Fields & LLM Prompts
*   **Structure:** `surface_container_lowest` background, 1px `outline` border. 
*   **Focus State:** Border changes to `primary` (2px thickness) with no outer glow. This mimics the rigid, precise behavior of high-end IDEs.
*   **Corner Radius:** Strictly `4px` to maintain the "Architect" aesthetic.

### Cards & Data Tables
*   **Container:** `surface_container_lowest` with a 1px `outline_variant` border.
*   **Header:** Separation is achieved by a background shift to `surface_container_low` in the header row, rather than a thick divider.
*   **Vertical Rhythm:** Use Spacing Scale `4` (0.9rem) for internal card padding to maintain a compact, professional density.

### Chips (Model Tags)
*   **Model Types:** Use `tertiary_container` (`#9dfd6d`) for "Active" models and `error_container` (`#fe8983`) for "Deprecated" or "Offline" models. Tags should use `label-sm` typography.

---

## 6. Do’s and Don’ts

### Do:
*   **DO** use strict 4px radius for all elements. Consistency in "roundedness" is what makes a flat design feel intentional rather than unfinished.
*   **DO** use white space (Spacing Scale `6` or `8`) to separate major dashboard widgets instead of heavy lines.
*   **DO** use `secondary` text color (`#5d5f63`) for helper text to create a clear visual hierarchy between "Data" and "Labels."

### Don't:
*   **DON'T** use any gradients or blurs. The system relies on pure hex values to communicate modernism.
*   **DON'T** use blue-purple tints. Stick strictly to the neutral grays and the specified primary blue.
*   **DON'T** use 100% black text. Use `on_surface` (`#2f3338`) to avoid "visual vibrating" on high-contrast screens.
*   **DON'T** exceed the 4px border radius. This is the hallmark of the "Clinical Architect" style.