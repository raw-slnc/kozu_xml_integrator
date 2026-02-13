"""
Link Settings Dialog for connecting preview parcels with QGIS main window layers.

Configures two matching modes:
- Attribute matching: single lot layer with oaza/parent/branch columns (default)
- Spatial matching: separate oaza boundary layer for geometry-based filtering
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

        # Match mode selector
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("照合方式:"))
        self.comboMatchMode = QComboBox()
        self.comboMatchMode.addItem("属性照合（大字カラムで絞り込み）", "attribute")
        self.comboMatchMode.addItem("空間照合（大字境界で絞り込み）", "spatial")
        mode_layout.addWidget(self.comboMatchMode)
        layout.addLayout(mode_layout)

        # Lot number layer group (always visible)
        lot_group = QGroupBox("地番レイヤー")
        lot_form = QFormLayout()
        self.comboLotLayer = QComboBox()
        self.comboMunicipalityColumn = QComboBox()
        self.comboLotOazaColumn = QComboBox()
        self.lblLotOazaColumn = QLabel("大字名カラム:")
        self.comboParentColumn = QComboBox()
        self.comboBranchColumn = QComboBox()
        lot_form.addRow("レイヤー:", self.comboLotLayer)
        lot_form.addRow("市町村名カラム:", self.comboMunicipalityColumn)
        lot_form.addRow(self.lblLotOazaColumn, self.comboLotOazaColumn)
        lot_form.addRow("親番カラム:", self.comboParentColumn)
        lot_form.addRow("枝番カラム:", self.comboBranchColumn)
        lot_group.setLayout(lot_form)
        layout.addWidget(lot_group)

        # Oaza boundary layer group (spatial mode only)
        self.oaza_group = QGroupBox("大字境界レイヤー")
        oaza_form = QFormLayout()
        self.comboOazaLayer = QComboBox()
        self.comboOazaColumn = QComboBox()
        oaza_form.addRow("レイヤー:", self.comboOazaLayer)
        oaza_form.addRow("大字名カラム:", self.comboOazaColumn)
        self.oaza_group.setLayout(oaza_form)
        layout.addWidget(self.oaza_group)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Connect signals
        self.comboMatchMode.currentIndexChanged.connect(self._on_mode_changed)
        self.comboOazaLayer.currentIndexChanged.connect(self._update_oaza_columns)
        self.comboLotLayer.currentIndexChanged.connect(self._update_lot_columns)

        # Set initial visibility
        self._on_mode_changed(0)

    def _on_mode_changed(self, index: int):
        """Toggle UI visibility based on matching mode."""
        mode = self.comboMatchMode.currentData()
        is_spatial = (mode == 'spatial')
        self.oaza_group.setVisible(is_spatial)
        self.lblLotOazaColumn.setVisible(not is_spatial)
        self.comboLotOazaColumn.setVisible(not is_spatial)

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
        self.comboMunicipalityColumn.clear()
        self.comboParentColumn.clear()
        self.comboBranchColumn.clear()
        self.comboLotOazaColumn.clear()
        layer = self._get_layer(self.comboLotLayer)
        if not layer:
            return
        self.comboMunicipalityColumn.addItem("（なし）", "")
        for field in layer.fields():
            self.comboMunicipalityColumn.addItem(field.name(), field.name())
            self.comboParentColumn.addItem(field.name())
            self.comboBranchColumn.addItem(field.name())
            self.comboLotOazaColumn.addItem(field.name())

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

        # Restore mode
        mode = cfg.get('match_mode', 'spatial')
        idx = self.comboMatchMode.findData(mode)
        if idx >= 0:
            self.comboMatchMode.setCurrentIndex(idx)
        self._on_mode_changed(self.comboMatchMode.currentIndex())

        # Restore lot layer (always)
        lot_name = cfg.get('lot_layer_name', '')
        if lot_name:
            idx = self.comboLotLayer.findText(lot_name)
            if idx >= 0:
                self.comboLotLayer.setCurrentIndex(idx)
                # Restore columns
                mcol = cfg.get('municipality_column', '')
                if mcol:
                    midx = self.comboMunicipalityColumn.findData(mcol)
                    if midx >= 0:
                        self.comboMunicipalityColumn.setCurrentIndex(midx)
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
                # Restore lot oaza column (attribute mode)
                if mode == 'attribute':
                    oaza_col = cfg.get('oaza_column', '')
                    if oaza_col:
                        oidx = self.comboLotOazaColumn.findText(oaza_col)
                        if oidx >= 0:
                            self.comboLotOazaColumn.setCurrentIndex(oidx)

        # Restore oaza layer (spatial mode)
        if mode == 'spatial':
            oaza_name = cfg.get('oaza_layer_name', '')
            if oaza_name:
                idx = self.comboOazaLayer.findText(oaza_name)
                if idx >= 0:
                    self.comboOazaLayer.setCurrentIndex(idx)
                    col = cfg.get('oaza_column', '')
                    if col:
                        cidx = self.comboOazaColumn.findText(col)
                        if cidx >= 0:
                            self.comboOazaColumn.setCurrentIndex(cidx)

    def get_config(self) -> Optional[dict]:
        """Return configuration dict, or None if incomplete."""
        mode = self.comboMatchMode.currentData()
        lot_layer = self._get_layer(self.comboLotLayer)
        if not lot_layer:
            return None

        parent_col = self.comboParentColumn.currentText()
        branch_col = self.comboBranchColumn.currentText()
        if not parent_col or not branch_col:
            return None

        municipality_col = self.comboMunicipalityColumn.currentData()
        config = {
            'match_mode': mode,
            'lot_layer_id': lot_layer.id(),
            'lot_layer_name': lot_layer.name(),
            'municipality_column': municipality_col or '',
            'parent_column': parent_col,
            'branch_column': branch_col,
        }

        if mode == 'attribute':
            oaza_col = self.comboLotOazaColumn.currentText()
            if not oaza_col:
                return None
            config['oaza_column'] = oaza_col
        else:  # spatial
            oaza_layer = self._get_layer(self.comboOazaLayer)
            if not oaza_layer:
                return None
            oaza_col = self.comboOazaColumn.currentText()
            if not oaza_col:
                return None
            config['oaza_layer_id'] = oaza_layer.id()
            config['oaza_layer_name'] = oaza_layer.name()
            config['oaza_column'] = oaza_col

        return config

    @staticmethod
    def save_to_settings(config: dict):
        """Persist link config to QSettings."""
        settings = QSettings()
        # Clear old keys first to avoid stale values from previous mode
        settings.remove('KozuXmlIntegrator/LinkConfig')
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
        for key in ['match_mode', 'oaza_layer_name', 'oaza_column',
                     'lot_layer_name', 'municipality_column',
                     'parent_column', 'branch_column']:
            val = settings.value(key, '')
            if val:
                config[key] = val
        settings.endGroup()
        return config
