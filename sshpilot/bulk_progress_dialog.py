"""
Progress Dialog for Bulk Operations
Shows real-time progress with detailed status and cancel functionality
"""

import logging
from typing import Optional
from gettext import gettext as _

from gi.repository import Gtk, Adw, GObject, GLib, Pango
from .bulk_operations import BulkOperationStatus, OperationResult, OperationType

logger = logging.getLogger(__name__)


class BulkProgressDialog(Adw.Window):
    """Progress dialog for bulk operations with real-time updates and cancel functionality"""
    
    __gsignals__ = {
        'cancel-requested': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }
    
    def __init__(self, parent_window, operation_status: BulkOperationStatus):
        super().__init__()
        
        self.operation_status = operation_status
        self.is_completed = False
        
        # Window setup
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_resizable(False)
        self.set_default_size(400, 300)
        
        # Set title based on operation type
        operation_name = {
            OperationType.CONNECT: _("Connecting to Group"),
            OperationType.DISCONNECT: _("Disconnecting from Group")
        }.get(operation_status.operation_type, _("Bulk Operation"))
        
        self.set_title(operation_name)
        
        # Build UI
        self._build_ui()
        self._update_display()
    
    def _build_ui(self):
        """Build the dialog UI"""
        
        # Main content box
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(24)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        
        # Header with operation description
        self.header_label = Gtk.Label()
        self.header_label.set_markup(f"<b>{self._get_operation_description()}</b>")
        self.header_label.set_halign(Gtk.Align.START)
        content_box.append(self.header_label)
        
        # Progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_hexpand(True)
        content_box.append(self.progress_bar)
        
        # Status summary
        summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        summary_box.set_halign(Gtk.Align.CENTER)
        
        # Successful count
        self.success_label = Gtk.Label()
        self.success_label.add_css_class("success")
        summary_box.append(self.success_label)
        
        # Failed count  
        self.failed_label = Gtk.Label()
        self.failed_label.add_css_class("error")
        summary_box.append(self.failed_label)
        
        # Elapsed time
        self.time_label = Gtk.Label()
        self.time_label.add_css_class("dim-label")
        summary_box.append(self.time_label)
        
        content_box.append(summary_box)
        
        # Scrolled window for detailed results
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(200)
        scrolled.set_vexpand(True)
        
        # Results list
        self.results_listbox = Gtk.ListBox()
        self.results_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results_listbox.add_css_class("boxed-list")
        scrolled.set_child(self.results_listbox)
        
        content_box.append(scrolled)
        
        # Button box
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.END)
        
        # Cancel/Close button
        self.action_button = Gtk.Button()
        self.action_button.add_css_class("pill")
        self.action_button.connect('clicked', self._on_action_button_clicked)
        button_box.append(self.action_button)
        
        content_box.append(button_box)
        
        # Set content
        self.set_content(content_box)
        
        # Add custom CSS
        self._add_custom_css()
    
    def _add_custom_css(self):
        """Add custom CSS for styling"""
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
        .success { color: @success_color; }
        .error { color: @error_color; }
        .warning { color: @warning_color; }
        .connection-row { padding: 8px 12px; }
        .connection-name { font-weight: bold; }
        .connection-status { font-size: 0.9em; }
        .connection-duration { font-size: 0.8em; opacity: 0.7; }
        """)
        
        style_context = self.get_style_context()
        style_context.add_provider(css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    
    def _get_operation_description(self) -> str:
        """Get human-readable operation description"""
        count = self.operation_status.total_count
        
        descriptions = {
            OperationType.CONNECT: _("Connecting to {count} connections...").format(count=count),
            OperationType.DISCONNECT: _("Disconnecting from {count} connections...").format(count=count)
        }
        
        return descriptions.get(self.operation_status.operation_type, 
                              _("Processing {count} connections...").format(count=count))
    
    def update_progress(self, status: BulkOperationStatus, latest_result: Optional[OperationResult] = None):
        """Update the progress display"""
        self.operation_status = status
        
        # Add new result to the list if provided
        if latest_result:
            self._add_result_row(latest_result)
        
        # Update all displays
        self._update_display()
    
    def _update_display(self):
        """Update all display elements"""
        status = self.operation_status
        
        # Update progress bar
        progress = status.progress_percentage / 100.0
        self.progress_bar.set_fraction(progress)
        
        if status.is_running:
            self.progress_bar.set_text(f"{status.completed_count}/{status.total_count}")
        else:
            if status.is_cancelled:
                self.progress_bar.set_text(_("Cancelled"))
            else:
                self.progress_bar.set_text(_("Completed"))
        
        # Update summary labels
        self.success_label.set_text(f"✓ {status.successful_count}")
        self.failed_label.set_text(f"✗ {status.failed_count}")
        
        elapsed = status.elapsed_time
        if elapsed > 0:
            self.time_label.set_text(f"{elapsed:.1f}s")
        else:
            self.time_label.set_text("")
        
        # Update action button
        if status.is_running:
            self.action_button.set_label(_("Cancel"))
            self.action_button.add_css_class("destructive-action")
        else:
            self.action_button.set_label(_("Close"))
            self.action_button.remove_css_class("destructive-action")
            self.is_completed = True
        
        # Update header if completed
        if not status.is_running:
            if status.is_cancelled:
                self.header_label.set_markup(f"<b>{_('Operation Cancelled')}</b>")
            else:
                success_rate = (status.successful_count / status.total_count * 100) if status.total_count > 0 else 0
                self.header_label.set_markup(f"<b>{_('Operation Completed')} ({success_rate:.0f}% successful)</b>")
    
    def _add_result_row(self, result: OperationResult):
        """Add a result row to the results list"""
        row = Gtk.ListBoxRow()
        row.add_css_class("connection-row")
        
        # Main container
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Connection name and status
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        # Connection name
        name_label = Gtk.Label(label=result.connection.nickname)
        name_label.set_halign(Gtk.Align.START)
        name_label.add_css_class("connection-name")
        header_box.append(name_label)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header_box.append(spacer)
        
        # Status icon and text
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        if result.success:
            status_icon = Gtk.Label(label="✓")
            status_icon.add_css_class("success")
            status_text = _("Success")
            status_label = Gtk.Label(label=status_text)
            status_label.add_css_class("success")
        else:
            status_icon = Gtk.Label(label="✗")
            status_icon.add_css_class("error")
            status_text = _("Failed")
            status_label = Gtk.Label(label=status_text)
            status_label.add_css_class("error")
        
        status_box.append(status_icon)
        status_box.append(status_label)
        
        # Duration
        if result.duration > 0:
            duration_label = Gtk.Label(label=f"({result.duration:.1f}s)")
            duration_label.add_css_class("connection-duration")
            status_box.append(duration_label)
        
        header_box.append(status_box)
        box.append(header_box)
        
        # Error message if failed
        if not result.success and result.error:
            error_label = Gtk.Label(label=result.error)
            error_label.set_halign(Gtk.Align.START)
            error_label.add_css_class("connection-status")
            error_label.add_css_class("error")
            error_label.set_wrap(True)
            error_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            box.append(error_label)
        
        row.set_child(box)
        
        # Insert at the top for newest first
        self.results_listbox.prepend(row)
        
        # Scroll to show the new result
        GLib.idle_add(self._scroll_to_top)
    
    def _scroll_to_top(self):
        """Scroll to show the newest result"""
        try:
            # Get the scrolled window
            scrolled = self.results_listbox.get_parent()
            if scrolled and isinstance(scrolled, Gtk.ScrolledWindow):
                adj = scrolled.get_vadjustment()
                adj.set_value(0)  # Scroll to top
        except Exception:
            pass
        return False
    
    def _on_action_button_clicked(self, button):
        """Handle action button click (Cancel/Close)"""
        if self.is_completed:
            self.close()
        else:
            # Request cancellation
            self.emit('cancel-requested')
            
            # Update button to show cancellation is in progress
            button.set_label(_("Cancelling..."))
            button.set_sensitive(False)
    
    def on_operation_completed(self):
        """Called when the operation is completed"""
        self.is_completed = True
        self._update_display()
        
        # Auto-close after 3 seconds if all operations were successful
        if (self.operation_status.failed_count == 0 and 
            not self.operation_status.is_cancelled):
            GLib.timeout_add_seconds(3, self._auto_close)
    
    def _auto_close(self):
        """Auto-close the dialog"""
        if self.is_completed and not self.is_destroyed():
            self.close()
        return False  # Don't repeat


class BulkProgressManager:
    """Manages bulk progress dialogs and coordinates with bulk operations"""
    
    def __init__(self, parent_window, bulk_operations_manager):
        self.parent_window = parent_window
        self.bulk_operations_manager = bulk_operations_manager
        self.current_dialog: Optional[BulkProgressDialog] = None
        
        # Connect to bulk operations signals
        self.bulk_operations_manager.connect('operation-started', self._on_operation_started)
        self.bulk_operations_manager.connect('operation-progress', self._on_operation_progress)
        self.bulk_operations_manager.connect('operation-completed', self._on_operation_completed)
        self.bulk_operations_manager.connect('connection-result', self._on_connection_result)
    
    def _on_operation_started(self, manager, status: BulkOperationStatus):
        """Handle operation started"""
        if self.current_dialog:
            self.current_dialog.close()
        
        self.current_dialog = BulkProgressDialog(self.parent_window, status)
        self.current_dialog.connect('cancel-requested', self._on_cancel_requested)
        self.current_dialog.present()
    
    def _on_operation_progress(self, manager, status: BulkOperationStatus):
        """Handle operation progress update"""
        if self.current_dialog:
            self.current_dialog.update_progress(status)
    
    def _on_operation_completed(self, manager, status: BulkOperationStatus):
        """Handle operation completed"""
        if self.current_dialog:
            self.current_dialog.on_operation_completed()
    
    def _on_connection_result(self, manager, status: BulkOperationStatus, result: OperationResult):
        """Handle individual connection result"""
        if self.current_dialog:
            self.current_dialog.update_progress(status, result)
    
    def _on_cancel_requested(self, dialog):
        """Handle cancel request from dialog"""
        self.bulk_operations_manager.cancel_current_operation()
    
    def close_current_dialog(self):
        """Close the current dialog if open"""
        if self.current_dialog:
            self.current_dialog.close()
            self.current_dialog = None