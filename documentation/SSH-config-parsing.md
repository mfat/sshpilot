SSH Config Parsing: A Definitive Guide
This document outlines the various scenarios and complexities involved in parsing OpenSSH configuration files (.ssh/config). The primary goal is to reliably differentiate between Connectable Hosts, which should be displayed in a user interface, and Configuration Rules, which apply settings in the background.

## 1. Core Concepts
### 🎯 Connectable Hosts
These are specific, named entries that a user can directly connect to. They represent a single, actionable server alias.

### 📜 Configuration Rules
These are patterns or conditional blocks that apply settings to a class of connections. They are not directly connectable and should be hidden from a primary server list.

## 2. Scenarios for Connectable Hosts
These entries should be parsed and presented to the user as valid connection targets.

### Scenario: Simple Host Alias
The most common use case. An alias (webapp) maps to a HostName.

Code snippet

Host webapp
  HostName 192.168.1.50
  User admin
  Port 2222
Classification: Connectable

Key Info: The alias is webapp. The actual host to connect to is 192.168.1.50.

### Scenario: Implicit Hostname
When HostName is not specified, the Host alias itself is the resolvable hostname.

Code snippet

Host prod-server.example.com
  User ubuntu
  IdentityFile ~/.ssh/prod.key
Classification: Connectable

Key Info: The alias and hostname are both prod-server.example.com. A parser that requires HostName to be present will incorrectly classify this as a rule.

### Scenario: Host with a Proxy or Bastion
These are connectable hosts that are only reachable through another server. The presence of ProxyJump or ProxyCommand does not make it a rule.

Code snippet

Host private-db
  HostName 10.0.50.15
  ProxyJump bastion-host
Classification: Connectable

Key Info: The user wants to connect to private-db. The SSH client handles the jump through bastion-host automatically.

### Scenario: Quoted Hostname
Host aliases can be quoted to include spaces or special characters.

Code snippet

Host "My Web Server"
  HostName web.internal
  User webadmin
Classification: Connectable

Key Info: The alias is "My Web Server". The parser must correctly handle the quotes.

## 3. Scenarios for Configuration Rules
These entries should be parsed to apply their settings but hidden from the main UI list.

### Scenario: Wildcard Patterns
A Host entry containing * or ? is a pattern, not a specific host.

Code snippet

Host *.internal.network
  User internal_user
  ForwardAgent yes
Classification: Rule

Reason: It applies to an infinite number of potential hosts (e.g., server1.internal.network, db.internal.network).

### Scenario: Multi-Pattern Declaration
A single Host line can contain multiple patterns, separated by spaces.

Code snippet

Host server* *.dev *.staging !prod
  User devuser
  StrictHostKeyChecking no
Classification: Rule

Reason: It defines a set of conditions for applying settings, not a single destination. The parser must split the line into individual patterns.

### Scenario: Negated Patterns
A pattern starting with ! is an exception. It matches everything except the pattern.

Code snippet

Host !bastion-host
  ProxyCommand /usr/bin/corp_proxy %h %p
Classification: Rule

Reason: This is a conditional rule for all connections that are not to bastion-host.

### Scenario: The Match Directive
The Match keyword introduces a conditional block that is more powerful than Host. It is always a rule.

Code snippet

# Applies settings to any connection by the 'root' user to any host
Match user root
  IdentityFile ~/.ssh/id_rsa_root
Classification: Rule

Reason: Match blocks are purely for applying settings based on criteria like user, host, or even the execution of a command.

### Scenario: The Include Directive
This directive tells the SSH client to load and parse another configuration file.

Code snippet

# Load all work-related configurations
Include ~/.ssh/config.d/work/*
Classification: Meta-Rule

Reason: This is a command for the parser itself. It is not a host or a rule, but it's essential for discovering all other hosts and rules.

## 4. Parser Requirements & Recommended Strategy
### ✅ Minimum Parser Requirements
A custom parser must be able to:

Differentiate Top-Level Keywords: Recognize Host, Match, and Include as distinct block starters.

Handle Multi-Patterns: Split Host lines into multiple patterns.

Detect Rules: Identify wildcards (*, ?) and negation (!).

Handle Quotes: Correctly parse quoted arguments.

Be Case-Insensitive: Treat keywords like Host, User, HostName as case-insensitive.

Process Include: Recursively read and parse included files.

### ⭐ The Gold Standard: Offload to ssh -G
The most robust and accurate method is to avoid writing a complex custom parser.

Discover Hosts: Use a simple regex like ^\s*Host\s+([^\s*?!]+)$ to find candidate aliases for your UI. This is a "best-effort" discovery step.

Evaluate Configuration: For a selected alias (e.g., webapp), run the command ssh -G webapp.

Parse the Output: The command produces a simple, definitive key value list of the final, effective configuration for that host. This output accounts for all Host blocks, Match blocks, Include files, and precedence rules.

This strategy guarantees 100% accuracy with the user's native SSH environment and is future-proof against new ssh_config features.