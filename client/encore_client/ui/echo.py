"""Echo cancellation on/off control for the workspace (R12, Jira EM-21)."""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class EchoControl(QWidget):
    """Shows the requested and effective echo cancellation mode and the switch.

    All decisions live in ``sensing.aec_mode.AecMode``; this widget only shows
    its state and forwards the button, like ``MicrophoneControl`` (R8).
    """

    def __init__(self, mode) -> None:
        super().__init__()
        self.mode = mode
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.button = QPushButton()
        self.button.clicked.connect(self._clicked)
        layout.addWidget(self.status, 1)
        layout.addWidget(self.button)
        self.refresh()

    def _clicked(self) -> None:
        self.mode.toggle()
        self.refresh()

    def refresh(self) -> None:
        self.mode.sync()
        self.status.setText(self.mode.status_text())
        self.button.setText(self.mode.button_text())
        self.button.setVisible(self.mode.available)
        self.button.setEnabled(self.mode.can_toggle)
