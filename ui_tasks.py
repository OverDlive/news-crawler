"""Small cancellable callbacks owned by a Tk window."""


class DebouncedCall:
    def __init__(self, widget, callback, delay=150):
        self.widget, self.callback, self.delay = widget, callback, delay
        self.timer = None
        widget.bind('<Destroy>', self._destroy, add='+')

    def __call__(self, *args):
        self.cancel()
        self.timer = self.widget.after(self.delay, self.flush)

    def cancel(self):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None

    def flush(self):
        self.cancel()
        self.callback()

    def _destroy(self, event):
        if event.widget is self.widget:
            self.cancel()
