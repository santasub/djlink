import sys
import netifaces as ni
from ipaddress import IPv4Address, IPv4Network
import logging


def _resolve_iface_name(name):
  """On Windows, netifaces-plus returns GUIDs instead of friendly names.
  Resolve a friendly name like 'Wi-Fi' or 'Ethernet' to a list of GUIDs.
  On Linux/macOS the name is returned as-is."""
  all_ifaces = ni.interfaces()

  # Exact match — works on Linux/macOS and if a GUID is passed on Windows
  if name in all_ifaces:
    return [name]

  if sys.platform == 'win32':
    # Look up friendly name -> GUID in the Windows registry
    try:
      import winreg
      key_path = r'SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}'
      base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
      matches = []
      idx = 0
      while True:
        try:
          guid = winreg.EnumKey(base, idx)
          idx += 1
          try:
            conn = winreg.OpenKey(base, guid + r'\Connection')
            friendly, _ = winreg.QueryValueEx(conn, 'Name')
            if friendly.lower() == name.lower():
              matches.append(guid)
          except OSError:
            pass
        except OSError:
          break
      if matches:
        logging.debug("Resolved interface '%s' -> %s", name, matches)
        return matches
    except Exception as exc:
      logging.debug("Registry interface resolution failed: %s", exc)

    # Fallback: maybe name is an IP address
    try:
      IPv4Address(name)
      for candidate in all_ifaces:
        for addr in ni.ifaddresses(candidate).get(ni.AF_INET, []):
          if addr.get('addr') == name:
            return [candidate]
    except ValueError:
      pass

  logging.warning("Interface '%s' not found, will scan all interfaces.", name)
  return all_ifaces


def guess_own_iface(match_ips, iface=None):
  if len(match_ips) == 0 and iface is None:
    return None

  ifaces = _resolve_iface_name(iface) if iface is not None else ni.interfaces()

  for resolved in ifaces:
    try:
      ifa = ni.ifaddresses(resolved)
    except ValueError:
      logging.warning("Interface %s not found or invalid.", resolved)
      continue

    if ni.AF_LINK not in ifa or len(ifa[ni.AF_LINK]) == 0:
      logging.debug("%s has no MAC address, skipped.", resolved)
      continue
    if ni.AF_INET not in ifa or len(ifa[ni.AF_INET]) == 0:
      logging.warning("%s has no IPv4 address.", resolved)
      continue

    mac = ifa[ni.AF_LINK][0]['addr']
    for addr in ifa[ni.AF_INET]:
      if 'addr' not in addr or 'netmask' not in addr:
        continue
      if iface is not None:
        # Specific interface requested — return first valid address found
        return resolved, addr['addr'], addr['netmask'], mac
      # Auto-detect — return if any match_ip is in this subnet
      net = IPv4Network(addr['addr'] + '/' + addr['netmask'], strict=False)
      if any(IPv4Address(ip) in net for ip in match_ips):
        return resolved, addr['addr'], addr['netmask'], mac

  return None
