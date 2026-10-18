"""AnimatedStackedWidget: QStackedWidget with smooth Fade-In + Subtle Slide page transitions."""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget, QWidget


class AnimatedStackedWidget(QStackedWidget):
    """QStackedWidget subclass that performs a smooth 220ms fade-in + 20px slide transition on page change."""

    def __init__(self, parent: QWidget | None = None, *, duration_ms: int = 220, slide_offset: int = 20) -> None:
        super().__init__(parent)
        self._duration_ms = int(duration_ms)
        self._slide_offset = int(slide_offset)
        self._anim_group: QParallelAnimationGroup | None = None
        self._active_target: QWidget | None = None

    @property
    def is_animating(self) -> bool:
        return (
            self._anim_group is not None
            and self._anim_group.state() == QParallelAnimationGroup.State.Running
        )

    def _cleanup_active_animation(self) -> None:
        if self._anim_group is not None:
            try:
                self._anim_group.stop()
            except RuntimeError:
                pass
            self._anim_group = None
        if self._active_target is not None:
            try:
                self._active_target.move(0, 0)
                self._active_target.setGraphicsEffect(None)
                self._active_target.show()
            except RuntimeError:
                pass
            self._active_target = None

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if index < 0 or index >= self.count():
            return
        prev_index = self.currentIndex()
        if index == prev_index and not self.is_animating:
            return

        self._cleanup_active_animation()

        target = self.widget(index)
        if target is None:
            super().setCurrentIndex(index)
            return

        # If widget is not visible yet (e.g., initial window construction), switch immediately without animation
        if not self.isVisible():
            super().setCurrentIndex(index)
            target.move(0, 0)
            return

        direction = 1 if index >= prev_index else -1
        start_pos = QPoint(direction * self._slide_offset, 0)
        end_pos = QPoint(0, 0)

        super().setCurrentIndex(index)
        target.resize(self.size())
        target.move(start_pos)

        effect = QGraphicsOpacityEffect(target)
        effect.setOpacity(0.0)
        target.setGraphicsEffect(effect)
        self._active_target = target

        fade_anim = QPropertyAnimation(effect, b"opacity", self)
        fade_anim.setDuration(self._duration_ms)
        fade_anim.setStartValue(0.0)
        fade_anim.setEndValue(1.0)
        fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        slide_anim = QPropertyAnimation(target, b"pos", self)
        slide_anim.setDuration(self._duration_ms)
        slide_anim.setStartValue(start_pos)
        slide_anim.setEndValue(end_pos)
        slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(fade_anim)
        group.addAnimation(slide_anim)
        group.finished.connect(self._on_transition_finished)
        self._anim_group = group
        group.start()

    def _on_transition_finished(self) -> None:
        if self._active_target is not None:
            try:
                self._active_target.move(0, 0)
                self._active_target.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self._active_target = None
        self._anim_group = None
