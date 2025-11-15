#!/usr/bin/env python3
import os,sys,json,shutil,subprocess,threading,socket,time,tarfile,tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict,Any,Optional,List
from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "Py86"
APP_VERSION = "beta 0.12"
CREDITS = "Made by ZodaciOS, https://github.com/ZodaciOS"
BASE_DIR = Path.home() / ".py86"
VMS_DIR = BASE_DIR / "vms"
EXPORTS_DIR = BASE_DIR / "exports"
DEFAULT_DISK_FORMAT = "raw"
SUPPORTED_NET_MODES = ["user (NAT)", "bridge", "host-only", "none"]

BASE_DIR.mkdir(parents=True, exist_ok=True)
VMS_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def find_qemu():
    candidates = ["qemu-system-x86_64", "qemu-system-x86_64.exe"]
    for c in candidates:
        path = shutil.which(c)
        if path:
            return path
    return None

DEFAULT_CONFIG = {
    "name": "py86-vm",
    "created": None,
    "version": APP_VERSION,
    "cpu_cores": 2,
    "memory_mib": 4096,
    "disk_gib": 32,
    "disk_format": DEFAULT_DISK_FORMAT,
    "efi": True,
    "boot_order": "cdrom,hd,menu",
    "iso_path": None,
    "extra_args": "",
    "network_mode": "user (NAT)",
    "enable_kvm": False,
    "graphics": "sdl",
    "display_embed": True,
    "advanced": {},
    "disks": [],
    "usb_passthrough": [],
    "nested_virt": False,
    "autostart": False,
    "vga": "virtio",
}

class VM:
    def __init__(self, name: str):
        self.name = name
        self.path = VMS_DIR / name
        self.config_path = self.path / "config.json"
        self.disk_dir = self.path / "disks"
        self.logs_dir = self.path / "logs"
        self.proc: Optional[subprocess.Popen] = None
        self.qmp_sock = None
        self.qmp_port = None
        self.display_window_id = None
        self._load_or_init()

    def _load_or_init(self):
        if not self.path.exists():
            self.path.mkdir(parents=True, exist_ok=True)
        if not self.disk_dir.exists():
            self.disk_dir.mkdir(parents=True, exist_ok=True)
        if not self.logs_dir.exists():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = dict(DEFAULT_CONFIG)
            cfg["name"] = self.name
            cfg["created"] = now_iso()
            self.save_config(cfg)
        self.config = cfg
        self._ensure_disks_list()

    def _ensure_disks_list(self):
        if "disks" not in self.config:
            self.config["disks"] = []
            self.save_config()

    def save_config(self, cfg: Optional[Dict[str, Any]] = None):
        if cfg is not None:
            self.config = cfg
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def create_disk(self, name: str, gib: int, fmt: str = "raw"):
        target = self.disk_dir / name
        qemu_img = shutil.which("qemu-img")
        if qemu_img and fmt != "raw":
            subprocess.run([qemu_img, "create", "-f", fmt, str(target), f"{gib}G"], check=True)
        else:
            with open(target, "wb") as f:
                f.truncate(gib * 1024**3)
        self.config.setdefault("disks", []).append({"path": str(target), "format": fmt})
        self.save_config()

    def ensure_primary_disk(self):
        disks = self.config.get("disks", [])
        if not disks:
            name = f"{self.name}-disk0.{self.config.get('disk_format','raw')}"
            self.create_disk(name, int(self.config.get("disk_gib", 32)), self.config.get("disk_format", "raw"))

    def export_bundle(self, dest: Path, include_iso: bool = False):
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(self.config_path, arcname=f"{self.name}/config.json")
            for d in self.config.get("disks", []):
                p = Path(d["path"])
                if p.exists():
                    tar.add(p, arcname=f"{self.name}/disks/{p.name}")
            if include_iso and self.config.get("iso_path"):
                iso = Path(self.config["iso_path"])
                if iso.exists():
                    tar.add(iso, arcname=f"{self.name}/iso/{iso.name}")
        return dest

    @classmethod
    def import_bundle(cls, bundle_path: Path):
        with tarfile.open(bundle_path, "r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise RuntimeError("empty bundle")
            root = members[0].name.split("/", 1)[0]
            target_dir = VMS_DIR / root
            if target_dir.exists():
                raise FileExistsError("vm exists")
            tar.extractall(path=VMS_DIR)
            return cls(root)

    def _allocate_qmp_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.config.setdefault("advanced", {})["qmp_port"] = port
        self.save_config()
        return port

    def build_qemu_cmd(self, embed_window_id: Optional[int] = None) -> List[str]:
        qemu = find_qemu()
        if not qemu:
            raise FileNotFoundError("qemu not found")
        self.ensure_primary_disk()
        cfg = self.config
        cmd = [qemu]
        machine = cfg.get("advanced", {}).get("machine", "q35")
        cmd += ["-machine", machine]
        if cfg.get("enable_kvm"):
            cmd += ["-enable-kvm"]
        if cfg.get("nested_virt"):
            cmd += ["-cpu", "host"]
        cmd += ["-smp", str(cfg.get("cpu_cores", 2))]
        cmd += ["-m", str(cfg.get("memory_mib", 4096))]
        for d in cfg.get("disks", []):
            path = d.get("path")
            fmt = d.get("format", "raw")
            drive_opt = f"file={path},format={fmt},if=virtio"
            cmd += ["-drive", drive_opt]
        iso = cfg.get("iso_path")
        if iso:
            cmd += ["-cdrom", str(iso)]
        cmd += ["-boot", cfg.get("boot_order", "cdrom,hd,menu")]
        graphics = cfg.get("graphics", "sdl")
        display_embed = cfg.get("display_embed", True)
        if graphics == "none":
            cmd += ["-nographic"]
        else:
            if graphics == "sdl":
                if display_embed and embed_window_id:
                    cmd += ["-display", f"sdl,window-id={embed_window_id}"]
                else:
                    cmd += ["-display", "sdl"]
            elif graphics == "gtk":
                cmd += ["-display", "gtk"]
            elif graphics == "vnc":
                vnc_port = cfg.get("advanced", {}).get("vnc_port")
                if not vnc_port:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.bind(("127.0.0.1", 0)); vnc_port = s.getsockname()[1]; s.close()
                    cfg.setdefault("advanced", {})["vnc_port"] = vnc_port; self.save_config()
                cmd += ["-vnc", f"127.0.0.1:{vnc_port - 5900}"]
            else:
                cmd += ["-display", graphics]
        nm = cfg.get("network_mode", "user (NAT)")
        if nm.startswith("user"):
            cmd += ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0"]
        elif nm == "bridge":
            tap = cfg.get("advanced", {}).get("tap_name", "tap0")
            cmd += ["-netdev", f"tap,id=net0,ifname={tap},script=no,downscript=no", "-device", "virtio-net-pci,netdev=net0"]
        elif nm == "host-only":
            cmd += ["-netdev", "user,id=net0,restrict=on", "-device", "virtio-net-pci,netdev=net0"]
        else:
            cmd += ["-net", "none"]
        qmp_port = cfg.get("advanced", {}).get("qmp_port")
        if not qmp_port:
            qmp_port = self._allocate_qmp_port()
        cmd += ["-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait"]
        if cfg.get("efi"):
            ovmf_code = cfg.get("advanced", {}).get("ovmf_code")
            if ovmf_code:
                cmd += ["-bios", ovmf_code]
        extra = cfg.get("extra_args", "")
        if extra:
            cmd += extra.split()
        logf = self.logs_dir / f"{self.name}.log"
        cmd += ["-D", str(logf)]
        return cmd

    def start(self, embed_window_id: Optional[int] = None, stdout_cb=None, stderr_cb=None, exit_cb=None):
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("already running")
        cmd = self.build_qemu_cmd(embed_window_id)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
        def read_stream(stream, cb):
            try:
                for line in iter(stream.readline, ""):
                    if cb:
                        cb(line)
                stream.close()
            except:
                pass
        if stdout_cb:
            threading.Thread(target=read_stream, args=(self.proc.stdout, stdout_cb), daemon=True).start()
        if stderr_cb:
            threading.Thread(target=read_stream, args=(self.proc.stderr, stderr_cb), daemon=True).start()
        def waitproc():
            rc = self.proc.wait()
            if exit_cb:
                exit_cb(rc)
        threading.Thread(target=waitproc, daemon=True).start()
        self.qmp_port = self.config.get("advanced", {}).get("qmp_port")
        threading.Thread(target=self._try_connect_qmp, daemon=True).start()

    def stop(self):
        try:
            if self.qmp_sock:
                try:
                    self.qmp_command({"execute": "system_powerdown"})
                    return
                except:
                    pass
            if self.proc:
                self.proc.terminate()
        except:
            pass

    def kill(self):
        if self.proc:
            self.proc.kill()

    def _try_connect_qmp(self, timeout=8):
        port = self.config.get("advanced", {}).get("qmp_port")
        if not port:
            return
        start = time.time()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        while time.time() - start < timeout:
            try:
                s.connect(("127.0.0.1", port))
                greet = s.recv(8192).decode("utf-8", "ignore")
                s.sendall((json.dumps({"execute": "qmp_capabilities"}) + "\n").encode("utf-8"))
                _ = s.recv(8192)
                self.qmp_sock = s
                return
            except:
                time.sleep(0.25)
        try:
            s.close()
        except:
            pass

    def qmp_command(self, cmd_obj: Dict[str, Any]) -> Dict[str, Any]:
        if not self.qmp_sock:
            raise RuntimeError("qmp not connected")
        data = (json.dumps(cmd_obj) + "\n").encode("utf-8")
        self.qmp_sock.sendall(data)
        resp = self.qmp_sock.recv(65536).decode("utf-8", "ignore")
        try:
            return json.loads(resp)
        except:
            return {"raw": resp}

class QemuCheckerDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - QEMU Check")
        self.setMinimumSize(600, 220)
        layout = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(f"Py86 requires qemu-system-x86_64 to run VMs. The application will attempt to locate qemu automatically.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.result_path_edit = QtWidgets.QLineEdit()
        self.result_path_edit.setPlaceholderText("Leave blank to auto-find qemu or paste full qemu-system-x86_64 path here")
        layout.addWidget(self.result_path_edit)
        btn_layout = QtWidgets.QHBoxLayout()
        self.install_btn = QtWidgets.QPushButton("Open QEMU Install Instructions")
        self.install_btn.clicked.connect(self._open_install_instructions)
        btn_layout.addWidget(self.install_btn)
        self.browse_btn = QtWidgets.QPushButton("Browse qemu binary")
        self.browse_btn.clicked.connect(self._browse)
        btn_layout.addWidget(self.browse_btn)
        layout.addLayout(btn_layout)
        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _open_install_instructions(self):
        import webbrowser
        webbrowser.open("https://www.qemu.org/download/")

    def _browse(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select qemu-system-x86_64 binary")
        if p:
            self.result_path_edit.setText(p)

    def get_path(self):
        if self.exec() == QtWidgets.QDialog.Accepted:
            val = self.result_path_edit.text().strip()
            if val:
                return val
            return None
        return None

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1400, 900)
        self._qemu_path = find_qemu()
        self._check_qemu_before_load()
        self.vms: Dict[str, VM] = {}
        self.selected_vm: Optional[VM] = None
        self._build_ui()
        self._load_vms()
        self._auto_start_if_configured()

    def _check_qemu_before_load(self):
        if self._qemu_path:
            return
        dlg = QemuCheckerDialog()
        user_path = dlg.get_path()
        if user_path:
            if Path(user_path).exists():
                self._qemu_path = user_path
                return
            else:
                QtWidgets.QMessageBox.critical(self, "Error", "Provided path does not exist. Please install QEMU or provide a correct path.")
                sys.exit(1)
        else:
            QtWidgets.QMessageBox.information(self, "QEMU Not Found", "QEMU not found. Please install qemu-system-x86_64. The UI will still run but you cannot boot VMs until QEMU is installed.")
            return

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)
        self.left_panel = QtWidgets.QFrame()
        self.left_panel.setMinimumWidth(320)
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        title = QtWidgets.QLabel(APP_NAME)
        title.setStyleSheet("font-size:20pt;font-weight:700;")
        left_layout.addWidget(title)
        credits = QtWidgets.QLabel(CREDITS)
        credits.setStyleSheet("font-size:8pt;color:gray;")
        left_layout.addWidget(credits)
        left_layout.addSpacing(4)
        self.vm_list_widget = QtWidgets.QListWidget()
        left_layout.addWidget(self.vm_list_widget)
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_new = QtWidgets.QPushButton("New VM")
        self.btn_import = QtWidgets.QPushButton("Import")
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        btn_row.addWidget(self.btn_new)
        btn_row.addWidget(self.btn_import)
        btn_row.addWidget(self.btn_refresh)
        left_layout.addLayout(btn_row)
        left_layout.addSpacing(8)
        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("Filter VMs...")
        left_layout.addWidget(self.search_box)
        left_layout.addStretch()
        layout.addWidget(self.left_panel)
        self.right_panel = QtWidgets.QFrame()
        right_layout = QtWidgets.QVBoxLayout(self.right_panel)
        header_row = QtWidgets.QHBoxLayout()
        self.vm_title_label = QtWidgets.QLabel("Select a VM")
        self.vm_title_label.setStyleSheet("font-size:16pt;font-weight:700;")
        header_row.addWidget(self.vm_title_label)
        header_row.addStretch()
        self.state_label = QtWidgets.QLabel("")
        header_row.addWidget(self.state_label)
        right_layout.addLayout(header_row)
        content_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.settings_widget = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(self.settings_widget)
        form_scroll = QtWidgets.QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_container = QtWidgets.QWidget()
        form_layout = QtWidgets.QFormLayout(form_container)
        self.name_edit = QtWidgets.QLineEdit()
        form_layout.addRow("Name", self.name_edit)
        self.cpu_spin = QtWidgets.QSpinBox()
        self.cpu_spin.setRange(1, 64)
        form_layout.addRow("CPU Cores", self.cpu_spin)
        self.mem_spin = QtWidgets.QSpinBox()
        self.mem_spin.setRange(128, 262144)
        self.mem_spin.setSingleStep(128)
        form_layout.addRow("Memory (MiB)", self.mem_spin)
        self.disk_spin = QtWidgets.QSpinBox()
        self.disk_spin.setRange(1, 4096)
        form_layout.addRow("Disk (GiB)", self.disk_spin)
        self.disk_format_combo = QtWidgets.QComboBox()
        self.disk_format_combo.addItems(["raw", "qcow2"])
        form_layout.addRow("Disk Format", self.disk_format_combo)
        self.efi_chk = QtWidgets.QCheckBox("Enable EFI (OVMF)")
        form_layout.addRow("", self.efi_chk)
        self.boot_edit = QtWidgets.QLineEdit()
        form_layout.addRow("Boot Order", self.boot_edit)
        iso_row = QtWidgets.QHBoxLayout()
        self.iso_label = QtWidgets.QLabel("No ISO attached")
        iso_row.addWidget(self.iso_label)
        self.iso_btn = QtWidgets.QPushButton("Attach ISO")
        iso_row.addWidget(self.iso_btn)
        form_layout.addRow("ISO", iso_row)
        self.net_combo = QtWidgets.QComboBox()
        self.net_combo.addItems(SUPPORTED_NET_MODES)
        form_layout.addRow("Network Mode", self.net_combo)
        self.kvm_chk = QtWidgets.QCheckBox("Enable KVM Acceleration")
        form_layout.addRow("", self.kvm_chk)
        self.nested_chk = QtWidgets.QCheckBox("Nested Virtualization (cpu=host)")
        form_layout.addRow("", self.nested_chk)
        self.graphics_combo = QtWidgets.QComboBox()
        self.graphics_combo.addItems(["sdl", "gtk", "vnc", "none"])
        form_layout.addRow("Graphics", self.graphics_combo)
        self.embed_chk = QtWidgets.QCheckBox("Embed display in UI (attempt)")
        self.embed_chk.setChecked(True)
        form_layout.addRow("", self.embed_chk)
        self.extra_edit = QtWidgets.QLineEdit()
        form_layout.addRow("Extra QEMU Args", self.extra_edit)
        self.advanced_btn = QtWidgets.QPushButton("Advanced Options")
        form_layout.addRow("", self.advanced_btn)
        self.save_config_btn = QtWidgets.QPushButton("Save Config")
        form_layout.addRow("", self.save_config_btn)
        form_container.setLayout(form_layout)
        form_scroll.setWidget(form_container)
        settings_layout.addWidget(form_scroll)
        self.settings_widget.setLayout(settings_layout)
        self.display_widget = QtWidgets.QWidget()
        self.display_widget.setMinimumSize(640, 360)
        display_layout = QtWidgets.QVBoxLayout(self.display_widget)
        self.display_placeholder = QtWidgets.QLabel("VM Display")
        self.display_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        display_layout.addWidget(self.display_placeholder)
        top_actions = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.reset_btn = QtWidgets.QPushButton("Reset")
        self.snapshot_btn = QtWidgets.QPushButton("Snapshot")
        top_actions.addWidget(self.start_btn)
        top_actions.addWidget(self.stop_btn)
        top_actions.addWidget(self.pause_btn)
        top_actions.addWidget(self.reset_btn)
        top_actions.addWidget(self.snapshot_btn)
        display_layout.addLayout(top_actions)
        self.qmp_cmd_edit = QtWidgets.QLineEdit()
        self.qmp_cmd_edit.setPlaceholderText('Enter raw QMP JSON or simple commands like "system_powerdown"')
        self.qmp_send_btn = QtWidgets.QPushButton("Send QMP")
        qmp_row = QtWidgets.QHBoxLayout()
        qmp_row.addWidget(self.qmp_cmd_edit)
        qmp_row.addWidget(self.qmp_send_btn)
        display_layout.addLayout(qmp_row)
        self.console_output = QtWidgets.QPlainTextEdit()
        self.console_output.setReadOnly(True)
        display_layout.addWidget(self.console_output)
        content_split.addWidget(self.settings_widget)
        content_split.addWidget(self.display_widget)
        right_layout.addWidget(content_split)
        bottom_row = QtWidgets.QHBoxLayout()
        self.export_btn = QtWidgets.QPushButton("Export VM")
        self.delete_btn = QtWidgets.QPushButton("Delete VM")
        bottom_row.addWidget(self.export_btn)
        bottom_row.addWidget(self.delete_btn)
        right_layout.addLayout(bottom_row)
        layout.addWidget(self.right_panel)
        self.left_panel.setLayout(left_layout)
        self.right_panel.setLayout(right_layout)
        self.btn_new.clicked.connect(self._on_new_vm)
        self.btn_import.clicked.connect(self._on_import_vm)
        self.btn_refresh.clicked.connect(self._load_vms)
        self.vm_list_widget.currentItemChanged.connect(self._on_vm_select)
        self.iso_btn.clicked.connect(self._on_attach_iso)
        self.save_config_btn.clicked.connect(self._on_save_config)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.reset_btn.clicked.connect(self._on_reset)
        self.pause_btn.clicked.connect(self._on_pause)
        self.snapshot_btn.clicked.connect(self._on_snapshot)
        self.qmp_send_btn.clicked.connect(self._on_qmp_send)
        self.export_btn.clicked.connect(self._on_export)
        self.delete_btn.clicked.connect(self._on_delete)
        self.search_box.textChanged.connect(self._on_search)

    def _load_vms(self):
        self.vms.clear()
        self.vm_list_widget.clear()
        for p in VMS_DIR.iterdir():
            if p.is_dir():
                try:
                    vm = VM(p.name)
                    self.vms[p.name] = vm
                    item = QtWidgets.QListWidgetItem(p.name)
                    self.vm_list_widget.addItem(item)
                except Exception:
                    pass

    def _on_new_vm(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "New VM", "VM name")
        if not ok or not text.strip():
            return
        name = text.strip()
        if (VMS_DIR / name).exists():
            QtWidgets.QMessageBox.warning(self, "Exists", "VM already exists")
            return
        vm = VM(name)
        vm.config.update(DEFAULT_CONFIG)
        vm.config["name"] = name
        vm.config["created"] = now_iso()
        vm.save_config()
        vm.create_disk()
        self._load_vms()
        items = self.vm_list_widget.findItems(name, QtCore.Qt.MatchExactly)
        if items:
            self.vm_list_widget.setCurrentItem(items[0])

    def _on_import_vm(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import VM bundle", str(Path.home()), "Py86 VM Bundle (*.tar.gz)")
        if not path:
            return
        try:
            vm = VM.import_bundle(Path(path))
            self._load_vms()
            QtWidgets.QMessageBox.information(self, "Imported", f"Imported {vm.name}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))

    def _on_vm_select(self, current, previous):
        if not current:
            return
        name = current.text()
        vm = self.vms.get(name)
        if not vm:
            return
        self.selected_vm = vm
        self._populate_settings(vm)

    def _populate_settings(self, vm: VM):
        cfg = vm.config
        self.vm_title_label.setText(cfg.get("name", vm.name))
        self.name_edit.setText(cfg.get("name", vm.name))
        self.cpu_spin.setValue(int(cfg.get("cpu_cores", 2)))
        self.mem_spin.setValue(int(cfg.get("memory_mib", 4096)))
        self.disk_spin.setValue(int(cfg.get("disk_gib", 32)))
        self.disk_format_combo.setCurrentText(cfg.get("disk_format", "raw"))
        self.efi_chk.setChecked(bool(cfg.get("efi", True)))
        self.boot_edit.setText(cfg.get("boot_order", "cdrom,hd,menu"))
        iso = cfg.get("iso_path")
        self.iso_label.setText(str(iso) if iso else "No ISO attached")
        self.net_combo.setCurrentText(cfg.get("network_mode", "user (NAT)"))
        self.kvm_chk.setChecked(bool(cfg.get("enable_kvm", False)))
        self.nested_chk.setChecked(bool(cfg.get("nested_virt", False)))
        self.graphics_combo.setCurrentText(cfg.get("graphics", "sdl"))
        self.embed_chk.setChecked(bool(cfg.get("display_embed", True)))
        self.extra_edit.setText(cfg.get("extra_args", ""))

    def _on_attach_iso(self):
        if not self.selected_vm:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select ISO", str(Path.home()), "ISO Files (*.iso);;All Files (*)")
        if not path:
            return
        self.selected_vm.config["iso_path"] = path
        self.selected_vm.save_config()
        self.iso_label.setText(path)

    def _on_save_config(self):
        if not self.selected_vm:
            return
        cfg = self.selected_vm.config
        cfg["name"] = self.name_edit.text().strip() or cfg.get("name")
        cfg["cpu_cores"] = int(self.cpu_spin.value())
        cfg["memory_mib"] = int(self.mem_spin.value())
        cfg["disk_gib"] = int(self.disk_spin.value())
        cfg["disk_format"] = self.disk_format_combo.currentText()
        cfg["efi"] = bool(self.efi_chk.isChecked())
        cfg["boot_order"] = self.boot_edit.text().strip() or cfg.get("boot_order")
        cfg["network_mode"] = self.net_combo.currentText()
        cfg["enable_kvm"] = bool(self.kvm_chk.isChecked())
        cfg["nested_virt"] = bool(self.nested_chk.isChecked())
        cfg["graphics"] = self.graphics_combo.currentText()
        cfg["display_embed"] = bool(self.embed_chk.isChecked())
        cfg["extra_args"] = self.extra_edit.text().strip()
        self.selected_vm.save_config(cfg)
        QtWidgets.QMessageBox.information(self, "Saved", "VM configuration saved")

    def _on_start(self):
        if not self.selected_vm:
            return
        try:
            embed_id = None
            if self.selected_vm.config.get("display_embed", True) and self.graphics_combo.currentText() == "sdl":
                embed_id = int(self.display_widget.winId())
            self.selected_vm.start(embed_window_id=embed_id, stdout_cb=self._append_stdout, stderr_cb=self._append_stdout, exit_cb=self._on_vm_exit)
            self.state_label.setText("Running")
        except FileNotFoundError:
            QtWidgets.QMessageBox.critical(self, "QEMU Not Found", "qemu-system-x86_64 not found. Please install QEMU or provide path.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Start Failed", str(e))

    def _on_stop(self):
        if not self.selected_vm:
            return
        try:
            self.selected_vm.stop()
            self.state_label.setText("Stopping")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Stop Failed", str(e))

    def _on_pause(self):
        if not self.selected_vm:
            return
        try:
            if self.selected_vm.qmp_sock:
                self.selected_vm.qmp_command({"execute": "stop"})
            else:
                QtWidgets.QMessageBox.warning(self, "QMP", "QMP not connected")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Pause Failed", str(e))

    def _on_reset(self):
        if not self.selected_vm:
            return
        try:
            if self.selected_vm.qmp_sock:
                self.selected_vm.qmp_command({"execute": "system_reset"})
            else:
                QtWidgets.QMessageBox.warning(self, "QMP", "QMP not connected")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Reset Failed", str(e))

    def _on_snapshot(self):
        if not self.selected_vm:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Snapshot", "Snapshot name")
        if not ok or not name:
            return
        try:
            if self.selected_vm.qmp_sock:
                self.selected_vm.qmp_command({"execute": "savevm", "arguments": {"name": name}})
                QtWidgets.QMessageBox.information(self, "Snapshot", "Snapshot saved")
            else:
                QtWidgets.QMessageBox.warning(self, "QMP", "QMP not connected")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Snapshot Failed", str(e))

    def _on_qmp_send(self):
        if not self.selected_vm:
            return
        txt = self.qmp_cmd_edit.text().strip()
        if not txt:
            return
        try:
            if txt.startswith("{"):
                obj = json.loads(txt)
            else:
                obj = {"execute": txt}
            if not self.selected_vm.qmp_sock:
                QtWidgets.QMessageBox.warning(self, "QMP", "QMP not connected")
                return
            resp = self.selected_vm.qmp_command(obj)
            self._append_stdout("QMP RESP: " + json.dumps(resp))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "QMP Error", str(e))

    def _append_stdout(self, line: str):
        QtCore.QMetaObject.invokeMethod(self.console_output, "appendPlainText", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, line.rstrip("\n")))

    def _on_vm_exit(self, rc):
        self._append_stdout(f"VM exited with code {rc}")
        self.state_label.setText("Stopped")

    def _on_export(self):
        if not self.selected_vm:
            return
        dest, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export VM", str(EXPORTS_DIR / f"{self.selected_vm.name}.tar.gz"), "Py86 Bundle (*.tar.gz)")
        if not dest:
            return
        try:
            self.selected_vm.export_bundle(Path(dest), include_iso=True)
            QtWidgets.QMessageBox.information(self, "Exported", f"Exported to {dest}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Export Failed", str(e))

    def _on_delete(self):
        if not self.selected_vm:
            return
        ok = QtWidgets.QMessageBox.question(self, "Delete VM", f"Delete VM {self.selected_vm.name}? This will remove VM files.")
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.rmtree(self.selected_vm.path)
            self._load_vms()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Delete Failed", str(e))

    def _on_attach_iso(self):
        if not self.selected_vm:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select ISO", str(Path.home()), "ISO Files (*.iso);;All Files (*)")
        if not path:
            return
        self.selected_vm.config["iso_path"] = path
        self.selected_vm.save_config()
        self.iso_label.setText(path)

    def _on_search(self, txt):
        for i in range(self.vm_list_widget.count()):
            it = self.vm_list_widget.item(i)
            it.setHidden(False)
            if txt.strip():
                if txt.lower() not in it.text().lower():
                    it.setHidden(True)

    def _auto_start_if_configured(self):
        for vm in list(self.vms.values()):
            if vm.config.get("autostart"):
                self.vm_list_widget.setCurrentItem(self.vm_list_widget.findItems(vm.name, QtCore.Qt.MatchExactly)[0])
                self._on_start()

def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
