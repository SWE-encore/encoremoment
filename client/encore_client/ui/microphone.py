"""Microphone on/off control for the workspace (R8, Jira EM-17)."""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class MicrophoneControl(QWidget):
    """Shows the actual microphone state and lets the user turn it off and on.

    All decisions live in ``sensing.microphone.MicrophoneSwitch``; this widget
    only shows its state and forwards the button.
    """

    def __init__(self, switch) -> None:
        super().__init__()
        self.switch = switch
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
        self.switch.toggle()
        self.refresh()

    def refresh(self) -> None:
        self.switch.sync()
        self.status.setText(self.switch.status_text())
        self.button.setText(self.switch.button_text())
        self.button.setEnabled(self.switch.can_toggle)
