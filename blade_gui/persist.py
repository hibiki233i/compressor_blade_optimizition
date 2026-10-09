"""Remember form inputs and layout between sessions in the GUI settings file.

Each bound widget is restored once from ``<key>`` and written back on every
change, so a crash or a killed process still keeps the last typed value.
Values that cannot be restored (stale combo entries, out-of-range numbers) are
silently ignored; nothing here is ever passed to the CLI without being shown.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox, QSplitter,
                               QTabWidget, QWidget)


def remember(ctx, key: str, widget: QWidget) -> None:
    """Restore ``widget`` from ``ctx`` settings and keep the stored value current."""
    if isinstance(widget, QSplitter):
        state = ctx.read_bytes(key)
        if state is not None:
            widget.restoreState(state)
        widget.splitterMoved.connect(lambda *_: ctx.write_setting(key, widget.saveState()))
        return
    stored = ctx.read_setting(key, None)
    if isinstance(widget, QLineEdit):
        if stored is not None:
            widget.setText(stored)
        widget.textChanged.connect(lambda text: ctx.write_setting(key, text))
    elif isinstance(widget, QComboBox) and widget.isEditable():
        if stored is not None:
            widget.setCurrentText(stored)
        widget.currentTextChanged.connect(lambda text: ctx.write_setting(key, text))
    elif isinstance(widget, QComboBox):
        index = widget.findData(stored) if stored is not None else -1
        if index >= 0:
            widget.setCurrentIndex(index)
        widget.currentIndexChanged.connect(lambda i: ctx.write_setting(key, str(widget.itemData(i))))
    elif isinstance(widget, QCheckBox):
        if stored is not None:
            widget.setChecked(stored.lower() in {"true", "1"})
        widget.toggled.connect(lambda checked: ctx.write_setting(key, "true" if checked else "false"))
    elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
        if stored is not None:
            try:
                number = type(widget.value())(float(stored))
            except ValueError:
                number = None
            if number is not None and widget.minimum() <= number <= widget.maximum():
                widget.setValue(number)
        widget.valueChanged.connect(lambda value: ctx.write_setting(key, repr(value)))
    elif isinstance(widget, QTabWidget):
        if stored is not None and stored.isdigit() and int(stored) < widget.count():
            widget.setCurrentIndex(int(stored))
        widget.currentChanged.connect(lambda i: ctx.write_setting(key, str(i)))
    else:
        raise TypeError(f"Cannot remember {type(widget).__name__}")
