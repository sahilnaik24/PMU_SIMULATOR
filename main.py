import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import tkinter.scrolledtext as scrolledtext
import logging
import threading
import traceback
import json
import random
import time  # For sleep and timestamps
from collections import deque

# Import PMU and Frame classes from the synchrophasor package
from synchrophasor.pmu import Pmu
from synchrophasor.frame import ConfigFrame2, DataFrame


# Constants
ph_v_conversion = int(300000.0 / 32768 * 100000)  # Voltage phasor conversion factor
ph_i_conversion = int(15000.0 / 32768 * 100000)  # Current phasor conversion factor

# Default Configuration
DEFAULT_CONFIG = {
    "pmu_id": 780,
    "data_rate": 5,
    "port": 4712,
    "ip": "127.0.0.1",
    "method": "tcp",
    "buffer": 2048,
    "log_level": "INFO",
    "station_name": "NSU Station",
    "time_base": 1000000,
    "phasor_num": 14,
    "analog_num": 33,
    "digital_num": 11,
    "nominal_freq": 50,
    "cfg_count": 1,
}

# Dictionary to manage PMU threads and instances
pmu_threads = {}
pmu_instances = {}
thread_lock = threading.Lock()

# Global dictionary to hold log messages per PMU
pmu_logs = {}  # key: pmu_name, value: list of log strings

# Configure logging to print to terminal
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# --- Custom Logging Handler for Tkinter Log Box (not used directly anymore) ---
class TextHandler(logging.Handler):
    """
    This class allows you to log messages to a Tkinter Text widget.
    (Now we use a periodic poll in the UI to load pmu_logs.)
    """
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(tk.END)
        self.text_widget.after(0, append)


# --- Custom LoggerAdapter for PMU Logging ---
class PMULoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        # Ensure extra contains the 'pmu' field.
        extra = kwargs.get("extra", {})
        extra["pmu"] = self.extra["pmu"]
        kwargs["extra"] = extra
        # Format the message using a formatter.
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        record = self.logger.makeRecord(
            self.logger.name, self.logger.level,
            fn="", lno=0, msg=msg, args=kwargs.get("args", ()),
            exc_info=kwargs.get("exc_info"), func=None
        )
        record.pmu = self.extra["pmu"]
        formatted = formatter.format(record)
        pmu = self.extra["pmu"]
        logs = pmu_logs.setdefault(pmu, [])
        logs.append(formatted)
        if len(logs) > 2000:
            del logs[:-2000]
        return msg, kwargs


# --- PMU Simulation Classes and Functions ---
class PMUSimulator:
    """Class to handle PMU simulation logic."""
    def __init__(self, config, pmu_name):
        self.config = config
        self.pmu_name = pmu_name
        self.pmu = None

    def start(self):
        try:
            # Generate channel names dynamically
            channel_names = (
                [f"Phasor{i+1}" for i in range(self.config["phasor_num"])]
                + [f"Analog{i+1}" for i in range(self.config["analog_num"])]
                + [f"Digital{i//16+1}_{i%16+1}" for i in range(16 * self.config["digital_num"])]
            )

            # Create ConfigFrame2
            ieee_cfg2_sample = ConfigFrame2(
                self.config["pmu_id"],
                self.config["time_base"],
                1,  # Number of PMUs included in data frame
                self.config["station_name"],
                self.config["port"],
                (False, False, True, False),  # Data format
                self.config["phasor_num"],
                self.config["analog_num"],
                self.config["digital_num"],
                channel_names,
                [(ph_v_conversion, "v") if i < int(self.config["phasor_num"] / 2) else (ph_i_conversion, "i")
                 for i in range(self.config["phasor_num"])],
                [(1, "pow") for _ in range(self.config["analog_num"])],
                [(0x0000, 0xffff) for _ in range(self.config["digital_num"])],
                self.config["nominal_freq"],
                self.config["cfg_count"],
                self.config["data_rate"],
            )

            # Initialize PMU
            self.pmu = Pmu(
                self.config["pmu_id"],
                self.config["data_rate"],
                self.config["port"],
                self.config["ip"],
                self.config["method"],
                self.config["buffer"],
                True,  # Set timestamps
            )
            self.pmu.logger.setLevel(self.config["log_level"])

            # Configure PMU
            self.pmu.set_configuration(ieee_cfg2_sample)
            self.pmu.set_header()
            self.pmu.set_id(self.config["pmu_id"])
            self.pmu.set_data_rate(self.config["data_rate"])

            # Create a PMU-specific logger adapter.
            self.logger = PMULoggerAdapter(logging.getLogger("PMUSimulator"), {"pmu": self.pmu_name})
            self.logger.info(f"Starting simulation for PMU {self.pmu_name} (ID: {self.config['pmu_id']}, Port: {self.config['port']}).")

            # Start PMU
            self.pmu.run()
            self.logger.info("PMU started successfully.")

            # Initialize connection state tracking
            connection_state = None  # Possible values: None, "waiting", "connected"

            # Infinite loop to generate and send data
            while self.pmu_name in pmu_threads:
                try:
                    if self.pmu.clients:
                        if connection_state != "connected":
                            self.logger.info("PMU connected: client(s) have connected.")
                            connection_state = "connected"
                        ieee_data_sample = DataFrame(
                            self.config["pmu_id"],
                            ("ok", True, "timestamp", False, False, False, 0, "<10", 0),
                            [(random.randint(14135, 14435), random.randint(-12176, 12176))
                            for _ in range(self.config["phasor_num"])],
                            random.randint(49, 51),  # Frequency
                            10,  # ROCOF
                            [random.randint(300, 400) for _ in range(self.config["analog_num"])],
                            [random.randint(0, 32767) for _ in range(self.config["digital_num"])],
                            ieee_cfg2_sample,
                        )
                        self.pmu.send(ieee_data_sample)
                        # self.logger.info("Data frame sent.")
                        time.sleep(1.0 / self.config["data_rate"])
                    else:
                        if connection_state != "waiting":
                            self.logger.info("PMU waiting for connection...")
                            connection_state = "waiting"
                        time.sleep(0.5)
                except Exception as loop_exc:
                    self.logger.error(f"Loop error: {loop_exc}\n{traceback.format_exc()}")
                    time.sleep(0.1)  # Prevent tight error loop
        except Exception as e:
            self.logger.error(f"Simulation error: {e}")
        finally:
            if self.pmu:
                self.pmu.stop()
                self.logger.info("PMU stopped.")
            with thread_lock:
                if self.pmu_name in pmu_instances:
                    del pmu_instances[self.pmu_name]


def start_simulation(config, pmu_name):
    """Start the PMU simulation in a separate thread."""
    simulator = PMUSimulator(config, pmu_name)
    with thread_lock:
        pmu_instances[pmu_name] = simulator
    simulator.start()

def validate_config(config):
    """Validate the PMU configuration."""
    if not isinstance(config["pmu_id"], int) or config["pmu_id"] < 0:
        raise ValueError("PMU ID must be a positive integer.")
    if not isinstance(config["data_rate"], int) or config["data_rate"] <= 0:
        raise ValueError("Data rate must be a positive integer.")
    if not isinstance(config["port"], int) or config["port"] < 0 or config["port"] > 65535:
        raise ValueError("Port must be a valid integer between 0 and 65535.")
    if not isinstance(config["phasor_num"], int) or config["phasor_num"] < 0:
        raise ValueError("Number of phasors must be a positive integer.")
    # Add more validations as needed...


# --- UI Application ---
class PMUSimulatorUI:
    """Class to handle the UI for the PMU Simulator."""
    def __init__(self, root):
        self.root = root
        self.root.title("PMU Simulator")
        self.root.geometry("800x600")
        self.root.resizable(True, True)

        # Load configurations from JSON file
        self.pmu_configs = {}
        self.load_config_from_file()

        # Track the current PMU and edit mode status
        self.current_pmu = None
        self.edit_mode = False
        self.entries = {}
        self.status_label = None  # Simulation status indicator

        # Create UI
        self.create_ui()

    def load_config_from_file(self):
        """Load PMU configurations from JSON file."""
        try:
            with open("pmu_configs.json", "r") as f:
                loaded_configs = json.load(f)
                self.pmu_configs = {}
                for pmu_name, config in loaded_configs.items():
                    merged_config = DEFAULT_CONFIG.copy()
                    merged_config.update(config)
                    self.pmu_configs[pmu_name] = merged_config
        except FileNotFoundError:
            self.pmu_configs = {"PMU 1": DEFAULT_CONFIG.copy()}
            self.save_config_to_file()
        except Exception as e:
            messagebox.showerror("Error", f"Config load error: {str(e)}")
            self.pmu_configs = {"PMU 1": DEFAULT_CONFIG.copy()}

    def save_config_to_file(self):
        """Save PMU configurations to JSON file."""
        try:
            with open("pmu_configs.json", "w") as f:
                json.dump(self.pmu_configs, f, indent=4)
        except Exception as e:
            messagebox.showerror("Error", f"Config save failed: {str(e)}")

    def create_ui(self):
        """Create the main UI for the PMU Simulator."""
        # Left side: Treeview for PMU devices and buttons
        self.tree_frame = tk.Frame(self.root, width=200)
        self.tree_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)

        self.tree = ttk.Treeview(self.tree_frame, selectmode="extended")
        self.tree.heading("#0", text="PMU Devices")
        self.tree.pack(fill=tk.Y, expand=True)

        self.import_button = tk.Button(self.tree_frame, text="Import Config", command=self.import_config, padx=5)
        self.import_button.pack(side=tk.TOP, fill=tk.X, pady=2)

        self.add_button = tk.Button(self.tree_frame, text="Add PMU", command=self.add_pmu_popup, padx=5)
        self.add_button.pack(side=tk.TOP, fill=tk.X, pady=2)

        self.edit_button = tk.Button(self.tree_frame, text="Edit PMU Configuration", command=self.enable_edit_mode, padx=5)
        self.edit_button.pack(side=tk.TOP, fill=tk.X, pady=2)

        self.delete_button = tk.Button(self.tree_frame, text="Delete PMU", command=self.delete_pmu, padx=5)
        self.delete_button.pack(side=tk.TOP, fill=tk.X, pady=2)

        self.populate_tree()

        # Right side: Frame for landing and management pages
        self.right_frame = tk.Frame(self.root)
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Show landing page by default
        self.show_landing_page()

    def import_config(self):
        """Import PMU configurations from a JSON file, enforcing atomic addition, overwriting on name match, and user-friendly error display."""
        filepath = filedialog.askopenfilename(
            title="Import PMU Config",
            filetypes=[("JSON files", "*.json")]
        )
        if not filepath:
            return

        # Load JSON
        try:
            with open(filepath, "r") as f:
                imported_configs = json.load(f)
        except json.JSONDecodeError as e:
            messagebox.showerror(
                "Error",
                f"Invalid JSON file: {e.msg} (Line {e.lineno}, Column {e.colno})"
            )
            return
        except Exception as e:
            messagebox.showerror("Error", f"Import failed: {e}")
            return

        # Prepare existing and new sets
        existing_names = set(self.pmu_configs.keys())
        existing_ids = {cfg['pmu_id'] for cfg in self.pmu_configs.values()}
        existing_ports = {cfg['port'] for cfg in self.pmu_configs.values()}
        new_ids = []
        new_ports = []
        errors = []
        overwritten = []

        # First pass: collect IDs, ports, and detect name overwrites
        for name, cfg in imported_configs.items():
            new_ids.append(cfg.get('pmu_id'))
            new_ports.append(cfg.get('port'))
            if name in existing_names:
                overwritten.append(name)

        # Validate IDs: conflicts with existing and duplicates in file
        for pmu_id in new_ids:
            if pmu_id in existing_ids:
                errors.append(f"Asset ID {pmu_id} conflicts with existing device.")
        for pmu_id in set(new_ids):
            count = new_ids.count(pmu_id)
            if count > 1:
                errors.append(f"Asset ID {pmu_id} appears {count} times in import file.")

        # Validate ports: conflicts with existing and duplicates in file
        for port in new_ports:
            if port in existing_ports:
                errors.append(f"Port {port} conflicts with existing device.")
        for port in set(new_ports):
            count = new_ports.count(port)
            if count > 1:
                errors.append(f"Port {port} appears {count} times in import file.")

        # Show errors, if any, in bullet list
        if errors:
            bullet_errors = "\n".join(f"• {err}" for err in errors)
            messagebox.showerror("Import validation failed", bullet_errors)
            return

        # Merge: overwrite names, add new
        for name, cfg in imported_configs.items():
            self.pmu_configs[name] = cfg

        self.save_config_to_file()
        self.populate_tree()

        # Build success message
        messages = []
        if overwritten:
            messages.append(
                "Overwritten configurations for: " + ", ".join(overwritten)
            )
        added = [n for n in imported_configs if n not in overwritten]
        if added:
            messages.append(
                "Added configurations for: " + ", ".join(added)
            )
        messagebox.showinfo("Success", "\n".join(messages))
    



    def populate_tree(self):
        """Populate the tree with PMU configurations."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        for pmu_name in self.pmu_configs:
            self.tree.insert("", "end", pmu_name, text=pmu_name)

    def show_landing_page(self):
        """Show the landing page."""
        for widget in self.right_frame.winfo_children():
            widget.destroy()

        tk.Label(self.right_frame, text="PMU Simulator Dashboard", font=("Arial", 16, "bold")).pack(pady=10)
        tk.Label(self.right_frame, text=f"Number of PMUs Configured: {len(self.pmu_configs)}", font=("Arial", 12)).pack(pady=5)
        running_pmus = list(pmu_threads.keys())
        tk.Label(self.right_frame, text=f"Number of PMUs Running: {len(running_pmus)}", font=("Arial", 12)).pack(pady=5)
        tk.Button(self.right_frame, text="Go to PMU Management", command=self.show_pmu_management, bg="blue", fg="white").pack(pady=10)

        # Scrollable PMU list with device info
        scroll_frame = tk.Frame(self.right_frame)
        scroll_frame.pack(pady=10, fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(scroll_frame, borderwidth=0, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        inner_frame = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner_frame, anchor="center")

        def on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner_frame.bind("<Configure>", on_frame_configure)

        for pmu_name, config in self.pmu_configs.items():
            color = "green" if pmu_name in pmu_threads else "red"
            item_frame = tk.Frame(inner_frame)
            item_frame.pack(fill="x", pady=2, padx=20)
            status_label = tk.Label(item_frame, text="●", font=("Arial", 12), fg=color)
            status_label.pack(side=tk.LEFT)
            info_text = f" {pmu_name} | ID: {config.get('pmu_id')} | Port: {config.get('port')}"
            name_label = tk.Label(item_frame, text=info_text, font=("Arial", 9), fg="gray20")
            name_label.pack(side=tk.LEFT)

    def show_pmu_management(self):
        """Show the PMU management page with configurations loaded from JSON."""
        for widget in self.right_frame.winfo_children():
            widget.destroy()
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        top_button_frame = tk.Frame(self.right_frame)
        top_button_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        tk.Button(top_button_frame, text="Back to Dashboard", command=self.show_landing_page, bg="gray", fg="white").pack(side=tk.LEFT, padx=10)
        container_frame = tk.Frame(self.right_frame)
        container_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        edit_control_frame = tk.Frame(container_frame)
        edit_control_frame.pack(fill=tk.X, padx=10, pady=5)
        self.save_button = tk.Button(edit_control_frame, text="Save", command=self.save_current_config, state="disabled", padx=5)
        self.save_button.pack(side=tk.LEFT, padx=5)
        self.config_frame = tk.Frame(container_frame, height=700)
        self.config_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0))
        if not self.tree.selection():
            children = self.tree.get_children()
            if children:
                self.tree.selection_set(children[0])
                self.tree.focus(children[0])
        selected_item = self.tree.focus()
        if selected_item:
            pmu_name = self.tree.item(selected_item, "text")
            self.disable_edit_mode()
            self.load_config(pmu_name)
        # Create log box (logs will be updated from the pmu_logs buffer)
        self.log_box = scrolledtext.ScrolledText(container_frame, state='disabled', height=15, font=("Arial", 9))
        self.log_box.pack(fill=tk.X, pady=(5, 0))
        status_frame = tk.Frame(container_frame)
        status_frame.pack(fill=tk.X, pady=(2, 0))
        self.status_label = tk.Label(status_frame, text="Status: Stopped", fg="red", anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=5)
        tk.Button(status_frame, text="Clear Logs", command=self.clear_logs, bg="#f0f0f0", activebackground="#e0e0e0").pack(side=tk.RIGHT, padx=5)
        # Start periodic log update
        self.update_log_text()

    def update_log_text(self):
        """Periodically update the log text widget with logs for the current PMU."""
        if self.current_pmu:
            logs = pmu_logs.get(self.current_pmu, [])
            self.log_box.configure(state='normal')
            self.log_box.delete('1.0', tk.END)
            self.log_box.insert(tk.END, "\n".join(logs))
            self.log_box.configure(state='disabled')
        self.root.after(1000, self.update_log_text)

    def clear_logs(self):
        if self.current_pmu:
            pmu_logs[self.current_pmu] = []
        self.log_box.configure(state='normal')
        self.log_box.delete('1.0', tk.END)
        self.log_box.configure(state='disabled')

    def on_tree_select(self, event):
        selected_item = self.tree.focus()
        if selected_item:
            pmu_name = self.tree.item(selected_item, "text")
            self.disable_edit_mode()
            self.load_config(pmu_name)

    def load_config(self, pmu_name):
        self.current_pmu = pmu_name
        for widget in self.config_frame.winfo_children():
            widget.destroy()
        config = self.pmu_configs.get(pmu_name, {})
        self.entries = {}
        keys = list(config.keys())
        num_params = len(keys)
        half = (num_params + 1) // 2
        for i, key in enumerate(keys):
            value = config[key]
            if i < half:
                row = i
                label_col = 0
                entry_col = 1
            else:
                row = i - half
                label_col = 2
                entry_col = 3
            lbl = tk.Label(self.config_frame, text=key.replace("_", " ").capitalize() + ":")
            lbl.grid(row=row, column=label_col, sticky=tk.W, pady=2, padx=5)
            entry = tk.Entry(self.config_frame, width=30)
            entry.insert(0, str(value))
            if not self.edit_mode:
                entry.config(state="disabled")
            entry.grid(row=row, column=entry_col, pady=2, padx=5)
            self.entries[key] = entry
        max_row = max(half, num_params - half)
        sim_row = max_row
        sim_frame = tk.Frame(self.config_frame)
        sim_frame.grid(row=sim_row, column=0, columnspan=4, pady=10)
        tk.Button(sim_frame, text="Start Simulation", command=self.start_selected_pmu, bg="green", fg="white").pack(side=tk.LEFT, padx=10)
        tk.Button(sim_frame, text="Stop Simulation", command=self.stop_selected_pmu, bg="red", fg="white").pack(side=tk.LEFT, padx=10)

    def update_status_indicator(self):
        if self.current_pmu:
            if self.current_pmu in pmu_threads:
                status_text = "Running"
                status_color = "green"
            else:
                status_text = "Stopped"
                status_color = "red"
            if self.status_label:
                self.status_label.config(text=f"Status: {status_text}", fg=status_color)

    def enable_edit_mode(self):
        self.edit_mode = True
        for key, entry in list(self.entries.items()):
            try:
                entry.config(state="normal")
            except tk.TclError:
                del self.entries[key]
        self.edit_button.config(state="disabled")
        self.save_button.config(state="normal")

    def disable_edit_mode(self):
        self.edit_mode = False
        for key, entry in list(self.entries.items()):
            try:
                entry.config(state="disabled")
            except tk.TclError:
                del self.entries[key]
        self.edit_button.config(state="normal")
        self.save_button.config(state="disabled")

    def save_current_config(self):
        pmu_name = self.current_pmu
        new_config = {}
        try:
            for key, entry in self.entries.items():
                try:
                    new_config[key] = type(DEFAULT_CONFIG[key])(entry.get())
                except tk.TclError:
                    continue
            for other_name, config in self.pmu_configs.items():
                if other_name != pmu_name:
                    if new_config["pmu_id"] == config["pmu_id"]:
                        messagebox.showerror("Error", "Device ID already exists in another PMU.")
                        return
                    if new_config["port"] == config["port"]:
                        messagebox.showerror("Error", "Port already exists in another PMU.")
                        return
            validate_config(new_config)
            self.pmu_configs[pmu_name] = new_config
            self.save_config_to_file()
            messagebox.showinfo("Success", f"Configuration for {pmu_name} saved successfully!")
            self.disable_edit_mode()
        except ValueError as e:
            messagebox.showerror("Error", f"Invalid configuration: {e}")

    def add_pmu_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("Add New PMU")
        popup.grab_set()
        frame = tk.Frame(popup, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)
        device_name_lbl = tk.Label(frame, text="Device Name:")
        device_name_lbl.grid(row=0, column=0, sticky="e", padx=5, pady=5)
        device_name_entry = tk.Entry(frame, width=30)
        device_name_entry.grid(row=0, column=1, padx=5, pady=5)
        entries = {}
        row = 1
        for key in DEFAULT_CONFIG:
            lbl = tk.Label(frame, text=key.replace("_", " ").capitalize() + ":")
            lbl.grid(row=row, column=0, sticky="e", padx=5, pady=5)
            entry = tk.Entry(frame, width=30)
            entry.grid(row=row, column=1, padx=5, pady=5)
            entries[key] = entry
            row += 1

        def save_new_pmu():
            new_config = {}
            device_name = device_name_entry.get().strip()
            if not device_name:
                messagebox.showerror("Error", "Please enter a device name.", parent=popup)
                return
            if device_name in self.pmu_configs:
                messagebox.showerror("Error", "A device with that name already exists.", parent=popup)
                return
            for key, entry in entries.items():
                value = entry.get().strip()
                if not value:
                    messagebox.showerror("Error", f"Please enter a value for {key}.", parent=popup)
                    return
                try:
                    new_config[key] = type(DEFAULT_CONFIG[key])(value)
                except Exception as e:
                    messagebox.showerror("Error", f"Invalid value for {key}: {e}", parent=popup)
                    return
            try:
                validate_config(new_config)
            except ValueError as e:
                messagebox.showerror("Error", f"Validation error: {e}", parent=popup)
                return
            for existing_config in self.pmu_configs.values():
                if new_config["pmu_id"] == existing_config["pmu_id"]:
                    messagebox.showerror("Error", "Device ID already exists.", parent=popup)
                    return
                if new_config["port"] == existing_config["port"]:
                    messagebox.showerror("Error", "Port already exists.", parent=popup)
                    return
            self.pmu_configs[device_name] = new_config
            self.save_config_to_file()
            self.populate_tree()
            messagebox.showinfo("Success", f"{device_name} added successfully with PMU ID {new_config['pmu_id']}!", parent=popup)
            popup.destroy()

        save_button = tk.Button(frame, text="Save", command=save_new_pmu, bg="green", fg="white")
        save_button.grid(row=row, column=0, columnspan=2, pady=10)

    def delete_pmu(self):
        selected_item = self.tree.focus()
        if not selected_item:
            messagebox.showerror("Error", "No PMU selected!")
            return
        pmu_name = self.tree.item(selected_item, "text")
        if messagebox.askyesno("Confirm Delete", f"Are you sure you want to delete {pmu_name}?"):
            if pmu_name in self.pmu_configs:
                del self.pmu_configs[pmu_name]
                self.save_config_to_file()
            self.tree.delete(selected_item)
            if self.current_pmu == pmu_name:
                for widget in self.config_frame.winfo_children():
                    widget.destroy()
            messagebox.showinfo("Deleted", f"{pmu_name} deleted successfully!")

    def start_selected_pmu(self):
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showerror("Error", "No PMU selected!")
            return
        started = []
        already_running = []
        for item in selected_items:
            pmu_name = self.tree.item(item, "text")
            if pmu_name in pmu_threads:
                already_running.append(pmu_name)
            else:
                config = self.pmu_configs[pmu_name]
                thread = threading.Thread(target=start_simulation, args=(config, pmu_name), daemon=True)
                with thread_lock:
                    pmu_threads[pmu_name] = thread
                thread.start()
                started.append(pmu_name)
        msg = ""
        if started:
            msg += f"Simulation started for: {', '.join(started)}."
        if already_running:
            msg += f"\nAlready running: {', '.join(already_running)}."
        messagebox.showinfo("Info", msg)
        self.update_status_indicator()

    def stop_selected_pmu(self):
        selected_item = self.tree.focus()
        if not selected_item:
            messagebox.showerror("Error", "No PMU selected!")
            return
        pmu_name = self.tree.item(selected_item, "text")
        if pmu_name not in pmu_threads:
            messagebox.showerror("Error", f"{pmu_name} is not running!")
            return
        with thread_lock:
            del pmu_threads[pmu_name]
        messagebox.showinfo("Success", f"Simulation for {pmu_name} stopped successfully!")
        self.update_status_indicator()

if __name__ == "__main__":
    root = tk.Tk()
    app = PMUSimulatorUI(root)
    root.mainloop()
