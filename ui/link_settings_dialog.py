"""
Link Settings Dialog for connecting preview parcels with QGIS main window layers.

Configures:
- Oaza (大字) layer and name column
- Lot number (地番) layer with parent and branch columns
"""
import logging
from typing import Optional

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QComboBox, QDialogButtonBox, QFormLayout
)
from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsProject, QgsVectorLayer

logger = logging.getLogger(__name__)


class LinkSettingsDialog(QDialog):
    """Dialog for configuring main window link settings."""

    def __init__(self, parent=None, current_config: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("メインウィンドウ連携設定")
        self.setMinimumWidth(400)

        self._current_config = current_config or {}
        self._setup_ui()
        self._populate_layers()
        self._restore_config()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Oaza layer group
        oaza_group = QGroupBox("大字レイヤー")
        oaza_form = QFormLayout()
        self.comboOazaLayer = QComboBox()
        self.comboOazaColumn = QComboBox()
        oaza_form.addRow("レイヤー:", self.comboOazaLayer)
        oaza_form.addRow("大字名カラム:", self.comboOazaColumn)
        oaza_group.setLayout(oaza_form)
        layout.addWidget(oaza_group)

        # Lot number layer group
        lot_group = QGroupBox("地番レイヤー")
        lot_form = QFormLayout()
        self.comboLotLayer = QComboBox()
        self.comboParentColumn = QComboBox()
        self.comboBranchColumn = QComboBox()
        lot_form.addRow("レイヤー:", self.comboLotLayer)
        lot_form.addRow("親番カラム:", self.comboParentColumn)
        lot_form.addRow("枝番カラム:", self.comboBranchColumn)
        lot_group.setLayout(lot_form)
        layout.addWidget(lot_group)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Connect layer changes to column updates
        self.comboOazaLayer.currentIndexChanged.connect(self._update_oaza_columns)
        self.comboLotLayer.currentIndexChanged.connect(self._update_lot_columns)

    def _populate_layers(self):
        """Populate layer combo boxes with vector layers from QGIS project."""
        self.comboOazaLayer.blockSignals(True)
        self.comboLotLayer.blockSignals(True)

        self.comboOazaLayer.clear()
        self.comboLotLayer.clear()
        self.comboOazaLayer.addItem("（選択してください）", None)
        self.comboLotLayer.addItem("（選択してください）", None)

        for layer_id, layer in QgsProject.instance().mapLayers().items():
            if isinstance(layer, QgsVectorLayer):
                self.comboOazaLayer.addItem(layer.name(), layer_id)
                self.comboLotLayer.addItem(layer.name(), layer_id)

        self.comboOazaLayer.blockSignals(False)
        self.comboLotLayer.blockSignals(False)

    def _update_oaza_columns(self, index: int):
        """Update oaza column combo when layer changes."""
        self.comboOazaColumn.clear()
        layer = self._get_layer(self.comboOazaLayer)
        if not layer:
            return
        for field in layer.fields():
            self.comboOazaColumn.addItem(field.name())

    def _update_lot_columns(self, index: int):
        """Update lot column combos when layer changes."""
        self.comboParentColumn.clear()
        self.comboBranchColumn.clear()
        layer = self._get_layer(self.comboLotLayer)
        if not layer:
            return
        for field in layer.fields():
            self.comboParentColumn.addItem(field.name())
            self.comboBranchColumn.addItem(field.name())

    def _get_layer(self, combo: QComboBox) -> Optional[QgsVectorLayer]:
        """Get QgsVectorLayer from combo box selection."""
        layer_id = combo.currentData()
        if not layer_id:
            return None
        return QgsProject.instance().mapLayer(layer_id)

    def _restore_config(self):
        """Restore previous configuration to combos."""
        cfg = self._current_config
        if not cfg:
            return

        # Restore oaza layer
        oaza_name = cfg.get('oaza_layer_name', '')
        if oaza_name:
            idx = self.comboOazaLayer.findText(oaza_name)
            if idx >= 0:
                self.comboOazaLayer.setCurrentIndex(idx)
                # Restore oaza column
                col = cfg.get('oaza_column', '')
                if col:
                    cidx = self.comboOazaColumn.findText(col)
                    if cidx >= 0:
                        self.comboOazaColumn.setCurrentIndex(cidx)

        # Restore lot layer
        lot_name = cfg.get('lot_layer_name', '')
        if lot_name:
            idx = self.comboLotLayer.findText(lot_name)
            if idx >= 0:
                self.comboLotLayer.setCurrentIndex(idx)
                # Restore columns
                pcol = cfg.get('parent_column', '')
                if pcol:
                    pidx = self.comboParentColumn.findText(pcol)
                    if pidx >= 0:
                        self.comboParentColumn.setCurrentIndex(pidx)
                bcol = cfg.get('branch_column', '')
                if bcol:
                    bidx = self.comboBranchColumn.findText(bcol)
                    if bidx >= 0:
                        self.comboBranchColumn.setCurrentIndex(bidx)

    def get_config(self) -> Optional[dict]:
        """Return configuration dict, or None if incomplete."""
        oaza_layer = self._get_layer(self.comboOazaLayer)
        lot_layer = self._get_layer(self.comboLotLayer)

        if not oaza_layer or not lot_layer:
            return None

        oaza_col = self.comboOazaColumn.currentText()
        parent_col = self.comboParentColumn.currentText()
        branch_col = self.comboBranchColumn.currentText()

        if not oaza_col or not parent_col or not branch_col:
            return None

        return {
            'oaza_layer_id': oaza_layer.id(),
            'oaza_layer_name': oaza_layer.name(),
            'oaza_column': oaza_col,
            'lot_layer_id': lot_layer.id(),
            'lot_layer_name': lot_layer.name(),
            'parent_column': parent_col,
            'branch_column': branch_col,
        }

    @staticmethod
    def save_to_settings(config: dict):
        """Persist link config to QSettings."""
        settings = QSettings()
        settings.beginGroup('KozuXmlIntegrator/LinkConfig')
        for key, val in config.items():
            settings.setValue(key, val)
        settings.endGroup()

    @staticmethod
    def load_from_settings() -> dict:
        """Load link config from QSettings."""
        settings = QSettings()
        settings.beginGroup('KozuXmlIntegrator/LinkConfig')
        config = {}
        for key in ['oaza_layer_name', 'oaza_column',
                     'lot_layer_name', 'parent_column', 'branch_column']:
            val = settings.value(key, '')
            if val:
                config[key] = val
        settings.endGroup()
        return config
