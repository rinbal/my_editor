// One step at a time: the navigation half of the install guide.
//
// index.html lists every step for every system. The Guide shows one system
// (a .track) and, within it, one step, with a step list on the side and
// Back / Next underneath. Which steps apply depends on <html data-mode> and
// <html data-package>, so switching to update mode or choosing the AppImage
// changes the list; call render() after changing either.

const FILTERS = ["mode", "package"];

export class Guide {
  constructor(doc) {
    this.doc = doc;
    this.root = doc.documentElement;
    this.tracks = new Map([...doc.querySelectorAll(".track")].map((track) => [track.dataset.os, track]));
    this.system = this.tracks.keys().next().value;
    this.index = 0;
    this.onChange = null;   // called with the step element after navigation
    for (const track of this.tracks.values()) this.#decorate(track);
  }

  /** Steps of a system that apply to the current mode and Linux package. */
  steps(system = this.system) {
    const track = this.tracks.get(system);
    return track ? [...track.querySelectorAll(".step")].filter((step) => this.#applies(step)) : [];
  }

  get current() {
    return this.steps()[this.index] || null;
  }

  show(system, stepId = null, options = {}) {
    if (this.tracks.has(system)) this.system = system;
    const index = this.steps().findIndex((step) => step.id === stepId);
    this.index = Math.max(0, index);
    this.render(options);
  }

  go(stepId, options = {}) {
    this.show(this.system, stepId, options);
  }

  next(options = {}) {
    this.index = Math.min(this.index + 1, this.steps().length - 1);
    this.render(options);
  }

  back(options = {}) {
    this.index = Math.max(this.index - 1, 0);
    this.render(options);
  }

  /** Redraws the page for the current system and step. With focus, moves
   *  keyboard and screen reader focus to the step's heading. */
  render({ focus = false, notify = true } = {}) {
    const steps = this.steps();
    this.index = Math.min(this.index, Math.max(steps.length - 1, 0));
    const current = steps[this.index];
    const track = this.tracks.get(this.system);

    for (const [system, element] of this.tracks) element.classList.toggle("is-active", system === this.system);
    for (const link of this.doc.querySelectorAll("[data-os-link]")) {
      link.setAttribute("aria-current", String(link.dataset.osLink === this.system));
    }
    for (const step of track.querySelectorAll(".step")) step.classList.toggle("is-current", step === current);
    steps.forEach((step, i) => {
      step.querySelector(".step-text").dataset.progress = `Step ${i + 1} of ${steps.length}`;
    });
    this.#renderStepper(track, steps);
    this.#renderNav(track, steps);

    if (!current) return;
    if (focus) this.#focus(current);
    if (notify && this.onChange) this.onChange(current);
  }

  #applies(step) {
    return FILTERS.every((key) => {
      const wanted = step.dataset[key];
      const actual = this.root.dataset[key];
      return !wanted || !actual || wanted === actual;
    });
  }

  #decorate(track) {
    const list = track.querySelector(".steps");

    const stepper = this.doc.createElement("ol");
    stepper.className = "stepper";
    stepper.setAttribute("aria-label", "Steps");
    list.before(stepper);

    const nav = this.doc.createElement("div");
    nav.className = "step-nav";
    nav.append(this.#button("Back", "back", "button"), this.#button("Next", "next", "button primary"));
    list.after(nav);
  }

  #button(text, action, className) {
    const button = this.doc.createElement("button");
    button.type = "button";
    button.className = className;
    button.dataset.nav = action;
    button.textContent = text;
    return button;
  }

  #renderStepper(track, steps) {
    const items = steps.map((step, i) => {
      const item = this.doc.createElement("li");
      if (i < this.index) item.className = "is-done";

      const button = this.doc.createElement("button");
      button.type = "button";
      button.dataset.go = step.id;
      if (i === this.index) button.setAttribute("aria-current", "step");

      const number = this.doc.createElement("span");
      number.className = "number";
      number.setAttribute("aria-hidden", "true");
      number.textContent = i < this.index ? "✓" : String(i + 1);

      const label = this.doc.createElement("span");
      label.className = "label";
      label.textContent = step.dataset.label;

      button.append(number, label);
      item.append(button);
      return item;
    });
    track.querySelector(".stepper").replaceChildren(...items);
  }

  #renderNav(track, steps) {
    track.querySelector('[data-nav="back"]').disabled = this.index === 0;
    track.querySelector('[data-nav="next"]').hidden = this.index >= steps.length - 1;
  }

  #focus(step) {
    const heading = step.querySelector("h3");
    heading?.focus({ preventScroll: true });
    const top = step.getBoundingClientRect().top;
    if (top < 0 || top > innerHeight * 0.6) {
      const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches;
      step.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
    }
  }
}
