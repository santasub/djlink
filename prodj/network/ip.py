import netifaces as ni
from ipaddress import IPv4Address, IPv4Network
import logging

def guess_own_iface(match_ips, iface=None):
  if len(match_ips) == 0 and iface is None:
    return None

  ifaces = ni.interfaces()
  if iface is not None:
    ifaces = [iface]

  for iface in ifaces:
    ifa = ni.ifaddresses(iface)

    if ni.AF_LINK not in ifa or len(ifa[ni.AF_LINK]) == 0:
      logging.debug("{} is has no MAC address, skipped.".format(iface))
      continue
    if ni.AF_INET not in ifa or len(ifa[ni.AF_INET]) == 0:
      logging.warning("{} has no IPv4 address".format(iface))
      continue

    mac = ifa[ni.AF_LINK][0]['addr']
    for addr in ifa[ni.AF_INET]:
      if 'addr' not in addr or 'netmask' not in addr:
        continue
      if iface is not None:
        return iface, addr['addr'], addr['netmask'], mac
      net = IPv4Network(addr['addr']+"/"+addr['netmask'], strict=False)
      if any([IPv4Address(ip) in net for ip in match_ips]):
        return iface, addr['addr'], addr['netmask'], mac

  return None
