#!/usr/bin/env python3
"""
Simple GTK4 window test
"""

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib

def on_activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Test Window")
    window.set_default_size(400, 300)
    
    label = Gtk.Label(label="Hello, GTK4!")
    window.set_child(label)
    
    window.present()
    print("Window should be visible now")

app = Gtk.Application(application_id="com.test.window")
app.connect('activate', on_activate)
app.run(None) 