/** jsdom has dialog elements, but not the browser's top-layer methods.
 * This double only supplies open/close state. Browser QA verifies native
 * focus containment and restoration; jsdom must not pretend to verify those.
 */
if (typeof HTMLDialogElement !== "undefined") {
  const prototype = HTMLDialogElement.prototype;
  if (!prototype.showModal) {
    prototype.showModal = function () { this.setAttribute("open", ""); };
    prototype.close = function (returnValue = "") {
      this.returnValue = returnValue;
      this.removeAttribute("open");
    };
  }
}
