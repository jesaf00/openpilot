#!/usr/bin/env python3
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple
from tqdm import tqdm
import capnp

import panda.python.uds as uds
from cereal import car
from common.params import Params
from selfdrive.car.ecu_addrs import EcuAddrBusType, get_ecu_addrs
from selfdrive.car.interfaces import get_interface_attr
from selfdrive.car.fingerprints import FW_VERSIONS
from selfdrive.car.isotp_parallel_query import IsoTpParallelQuery
from system.swaglog import cloudlog

Ecu = car.CarParams.Ecu
ESSENTIAL_ECUS = [Ecu.engine, Ecu.eps, Ecu.abs, Ecu.fwdRadar, Ecu.fwdCamera, Ecu.vsa]
FUZZY_EXCLUDE_ECUS = [Ecu.fwdCamera, Ecu.fwdRadar, Ecu.eps, Ecu.debug]

FW_QUERY_CONFIGS = get_interface_attr('FW_QUERY_CONFIG', ignore_none=True)
VERSIONS = get_interface_attr('FW_VERSIONS', ignore_none=True)

MODEL_TO_BRAND = {c: b for b, e in VERSIONS.items() for c in e}
REQUESTS = [(brand, config, r) for brand, config in FW_QUERY_CONFIGS.items() for r in config.requests]


def chunks(l, n=128):
  for i in range(0, len(l), n):
    yield l[i:i + n]


def is_brand(brand: str, filter_brand: Optional[str]) -> bool:
  """Returns if brand matches filter_brand or no brand filter is specified"""
  return filter_brand is None or brand == filter_brand


def build_fw_dict(fw_versions: List[capnp.lib.capnp._DynamicStructBuilder],
                  filter_brand: Optional[str] = None) -> Dict[Tuple[int, Optional[int]], Set[bytes]]:
  fw_versions_dict = defaultdict(set)
  for fw in fw_versions:
    if is_brand(fw.brand, filter_brand) and not fw.logging:
      sub_addr = fw.subAddress if fw.subAddress != 0 else None
      fw_versions_dict[(fw.address, sub_addr)].add(fw.fwVersion)
  return dict(fw_versions_dict)


def get_brand_addrs() -> Dict[str, Set[Tuple[int, Optional[int]]]]:
  brand_addrs: DefaultDict[str, Set[Tuple[int, Optional[int]]]] = defaultdict(set)
  for brand, cars in VERSIONS.items():
    # Add ecus in database + extra ecus to match against
    brand_addrs[brand] |= {(addr, sub_addr) for _, addr, sub_addr in FW_QUERY_CONFIGS[brand].extra_ecus}
    for fw in cars.values():
      brand_addrs[brand] |= {(addr, sub_addr) for _, addr, sub_addr in fw.keys()}
  return dict(brand_addrs)


def match_fw_to_car_fuzzy(fw_versions_dict, log=True, exclude=None):
  """Do a fuzzy FW match. This function will return a match, and the number of firmware version
  that were matched uniquely to that specific car. If multiple ECUs uniquely match to different cars
  the match is rejected."""

  # Build lookup table from (addr, sub_addr, fw) to list of candidate cars
  all_fw_versions = defaultdict(list)
  for candidate, fw_by_addr in FW_VERSIONS.items():
    if candidate == exclude:
      continue

    for addr, fws in fw_by_addr.items():
      # These ECUs are known to be shared between models (EPS only between hybrid/ICE version)
      # Getting this exactly right isn't crucial, but excluding camera and radar makes it almost
      # impossible to get 3 matching versions, even if two models with shared parts are released at the same
      # time and only one is in our database.
      if addr[0] in FUZZY_EXCLUDE_ECUS:
        continue
      for f in fws:
        all_fw_versions[(addr[1], addr[2], f)].append(candidate)

  matched_ecus = set()
  candidate = None
  for addr, versions in fw_versions_dict.items():
    ecu_key = (addr[0], addr[1])
    for version in versions:
      # All cars that have this FW response on the specified address
      candidates = all_fw_versions[(*ecu_key, version)]

      if len(candidates) == 1:
        matched_ecus.add(ecu_key)
        if candidate is None:
          candidate = candidates[0]
        # We uniquely matched two different cars. No fuzzy match possible
        elif candidate != candidates[0]:
          return set()

  # Note that it is possible to match to a candidate without all its ECUs being present
  # if there are enough matches. FIXME: parameterize this or require all ECUs to exist like exact matching
  if len(matched_ecus) >= 2:
    if log:
      cloudlog.error(f"Fingerprinted {candidate} using fuzzy match. {len(matched_ecus)} matching ECUs")
    return {candidate}
  else:
    return set()


def match_fw_to_car_exact(fw_versions_dict, log=True) -> Set[str]:
  """Do an exact FW match. Returns all cars that match the given
  FW versions for a list of "essential" ECUs. If an ECU is not considered
  essential the FW version can be missing to get a fingerprint, but if it's present it
  needs to match the database."""
  invalid = []
  candidates = FW_VERSIONS

  for candidate, fws in candidates.items():
    config = FW_QUERY_CONFIGS[MODEL_TO_BRAND[candidate]]
    for ecu, expected_versions in fws.items():
      ecu_type = ecu[0]
      addr = ecu[1:]

      found_versions = fw_versions_dict.get(addr, set())
      if not len(found_versions):
        # Some models can sometimes miss an ecu, or show on two different addresses
        if candidate in config.non_essential_ecus.get(ecu_type, []):
          continue

        # Ignore non essential ecus
        if ecu_type not in ESSENTIAL_ECUS:
          continue

      # Virtual debug ecu doesn't need to match the database
      if ecu_type == Ecu.debug:
        continue

      if not any([found_version in expected_versions for found_version in found_versions]):
        invalid.append(candidate)
        break

  return set(candidates.keys()) - set(invalid)


def match_fw_to_car(fw_versions, allow_exact=True, allow_fuzzy=True, log=True):
  # Try exact matching first
  exact_matches = []
  if allow_exact:
    exact_matches = [(True, match_fw_to_car_exact)]
  if allow_fuzzy:
    exact_matches.append((False, match_fw_to_car_fuzzy))

  for exact_match, match_func in exact_matches:
    # For each brand, attempt to fingerprint using all FW returned from its queries
    matches = set()
    for brand in VERSIONS.keys():
      fw_versions_dict = build_fw_dict(fw_versions, filter_brand=brand)
      matches |= match_func(fw_versions_dict, log=log)

    if len(matches):
      return exact_match, matches

  return True, set()


def get_present_ecus(logcan, sendcan, num_pandas=1) -> Set[EcuAddrBusType]:
  params = Params()
  # queries are split by OBD multiplexing mode
  queries: Dict[bool, List[List[EcuAddrBusType]]] = {True: [], False: []}
  parallel_queries: Dict[bool, List[EcuAddrBusType]] = {True: [], False: []}
  responses = set()

  for brand, config, r in REQUESTS:
    # Skip query if no panda available
    if r.bus > num_pandas * 4 - 1:
      continue

    for brand_versions in VERSIONS[brand].values():
      for ecu_type, addr, sub_addr in list(brand_versions) + config.extra_ecus:
        # Only query ecus in whitelist if whitelist is not empty
        if len(r.whitelist_ecus) == 0 or ecu_type in r.whitelist_ecus:
          a = (addr, sub_addr, r.bus)
          # Build set of queries
          if sub_addr is None:
            if a not in parallel_queries[r.obd_multiplexing]:
              parallel_queries[r.obd_multiplexing].append(a)
          else:  # subaddresses must be queried one by one
            if [a] not in queries[r.obd_multiplexing]:
              queries[r.obd_multiplexing].append([a])

          # Build set of expected responses to filter
          response_addr = uds.get_rx_addr_for_tx_addr(addr, r.rx_offset)
          responses.add((response_addr, sub_addr, r.bus))

  for obd_multiplexing in queries:
    queries[obd_multiplexing].insert(0, parallel_queries[obd_multiplexing])

  ecu_responses = set()
  for obd_multiplexing in queries:
    set_obd_multiplexing(params, obd_multiplexing)
    for query in queries[obd_multiplexing]:
      ecu_responses.update(get_ecu_addrs(logcan, sendcan, set(query), responses, timeout=0.1))
  return ecu_responses


def get_brand_ecu_matches(ecu_rx_addrs):
  """Returns dictionary of brands and matches with ECUs in their FW versions"""

  brand_addrs = get_brand_addrs()
  brand_matches = {brand: set() for brand, _, _ in REQUESTS}

  brand_rx_offsets = set((brand, r.rx_offset) for brand, _, r in REQUESTS)
  for addr, sub_addr, _ in ecu_rx_addrs:
    # Since we can't know what request an ecu responded to, add matches for all possible rx offsets
    for brand, rx_offset in brand_rx_offsets:
      a = (uds.get_rx_addr_for_tx_addr(addr, -rx_offset), sub_addr)
      if a in brand_addrs[brand]:
        brand_matches[brand].add(a)

  return brand_matches


def set_obd_multiplexing(params: Params, obd_multiplexing: bool):
  if params.get_bool("ObdMultiplexingEnabled") != obd_multiplexing:
    cloudlog.warning(f"Setting OBD multiplexing to {obd_multiplexing}")
    params.remove("ObdMultiplexingChanged")
    params.put_bool("ObdMultiplexingEnabled", obd_multiplexing)
    params.get_bool("ObdMultiplexingChanged", block=True)
    cloudlog.warning("OBD multiplexing set successfully")


def get_fw_versions_ordered(logcan, sendcan, ecu_rx_addrs, timeout=0.1, num_pandas=1, debug=False, progress=False) -> \
  List[capnp.lib.capnp._DynamicStructBuilder]:
  """Queries for FW versions ordering brands by likelihood, breaks when exact match is found"""

  all_car_fw = []
  brand_matches = get_brand_ecu_matches(ecu_rx_addrs)

  for brand in sorted(brand_matches, key=lambda b: len(brand_matches[b]), reverse=True):
    # Skip this brand if there are no matching present ECUs
    if not len(brand_matches[brand]):
      continue

    car_fw = get_fw_versions(logcan, sendcan, query_brand=brand, timeout=timeout, num_pandas=num_pandas, debug=debug, progress=progress)
    all_car_fw.extend(car_fw)
    # Try to match using FW returned from this brand only
    matches = match_fw_to_car_exact(build_fw_dict(car_fw))
    if len(matches) == 1:
      break

  return all_car_fw


def get_fw_versions(logcan, sendcan, query_brand=None, extra=None, timeout=0.1, num_pandas=1, debug=False, progress=False) -> \
  List[capnp.lib.capnp._DynamicStructBuilder]:
  versions = VERSIONS.copy()
  params = Params()

  # Each brand can define extra ECUs to query for data collection
  for brand, config in FW_QUERY_CONFIGS.items():
    versions[brand]["debug"] = {ecu: [] for ecu in config.extra_ecus}

  if query_brand is not None:
    versions = {query_brand: versions[query_brand]}

  if extra is not None:
    versions.update(extra)

  # Extract ECU addresses to query from fingerprints
  # ECUs using a subaddress need be queried one by one, the rest can be done in parallel
  addrs = []
  parallel_addrs = []
  ecu_types = {}

  for brand, brand_versions in versions.items():
    for ecu in brand_versions.values():
      for ecu_type, addr, sub_addr in ecu.keys():
        a = (brand, addr, sub_addr)
        if a not in ecu_types:
          ecu_types[a] = ecu_type

        if sub_addr is None:
          if a not in parallel_addrs:
            parallel_addrs.append(a)
        else:
          if [a] not in addrs:
            addrs.append([a])

  addrs.insert(0, parallel_addrs)

  # Get versions and build capnp list to put into CarParams
  car_fw = []
  requests = [(brand, config, r) for brand, config, r in REQUESTS if is_brand(brand, query_brand)]
  for addr in tqdm(addrs, disable=not progress):
    for addr_chunk in chunks(addr):
      for brand, config, r in requests:
        # Skip query if no panda available
        if r.bus > num_pandas * 4 - 1:
          continue

        # Toggle OBD multiplexing for each request
        if r.bus % 4 == 1:
          set_obd_multiplexing(params, r.obd_multiplexing)

        try:
          query_addrs = [(a, s) for (b, a, s) in addr_chunk if b in (brand, 'any') and
                         (len(r.whitelist_ecus) == 0 or ecu_types[(b, a, s)] in r.whitelist_ecus)]

          if query_addrs:
            query = IsoTpParallelQuery(sendcan, logcan, r.bus, query_addrs, r.request, r.response, r.rx_offset, debug=debug)
            for (tx_addr, sub_addr), version in query.get_data(timeout).items():
              f = car.CarParams.CarFw.new_message()

              f.ecu = ecu_types.get((brand, tx_addr, sub_addr), Ecu.unknown)
              f.fwVersion = version
              f.address = tx_addr
              f.responseAddress = uds.get_rx_addr_for_tx_addr(tx_addr, r.rx_offset)
              f.request = r.request
              f.brand = brand
              f.bus = r.bus
              f.logging = r.logging or (f.ecu, tx_addr, sub_addr) in config.extra_ecus
              f.obdMultiplexing = r.obd_multiplexing

              if sub_addr is not None:
                f.subAddress = sub_addr

              car_fw.append(f)
        except Exception:
          cloudlog.exception("FW query exception")

  return car_fw


if __name__ == "__main__":
  import time
  import argparse
  import cereal.messaging as messaging
  from selfdrive.car.vin import get_vin

  parser = argparse.ArgumentParser(description='Get firmware version of ECUs')
  parser.add_argument('--scan', action='store_true')
  parser.add_argument('--debug', action='store_true')
  parser.add_argument('--brand', help='Only query addresses/with requests for this brand')
  args = parser.parse_args()

  logcan = messaging.sub_sock('can')
  pandaStates_sock = messaging.sub_sock('pandaStates')
  sendcan = messaging.pub_sock('sendcan')

  extra: Any = None
  if args.scan:
    extra = {}
    # Honda
    for i in range(256):
      extra[(Ecu.unknown, 0x18da00f1 + (i << 8), None)] = []
      extra[(Ecu.unknown, 0x700 + i, None)] = []
      extra[(Ecu.unknown, 0x750, i)] = []
    extra = {"any": {"debug": extra}}

  time.sleep(1.)
  num_pandas = len(messaging.recv_one_retry(pandaStates_sock).pandaStates)

  t = time.time()
  print("Getting vin...")
  vin_rx_addr, vin = get_vin(logcan, sendcan, 1, retry=10, debug=args.debug)
  print(f'RX: {hex(vin_rx_addr)}, VIN: {vin}')
  print(f"Getting VIN took {time.time() - t:.3f} s")
  print()

  t = time.time()
  fw_vers = get_fw_versions(logcan, sendcan, query_brand=args.brand, extra=extra, num_pandas=num_pandas, debug=args.debug, progress=True)
  _, candidates = match_fw_to_car(fw_vers)

  print()
  print("Found FW versions")
  print("{")
  padding = max([len(fw.brand) for fw in fw_vers] or [0])
  for version in fw_vers:
    subaddr = None if version.subAddress == 0 else hex(version.subAddress)
    print(f"  Brand: {version.brand:{padding}}, bus: {version.bus} - (Ecu.{version.ecu}, {hex(version.address)}, {subaddr}): [{version.fwVersion}]")
  print("}")

  print()
  print("Possible matches:", candidates)
  print(f"Getting fw took {time.time() - t:.3f} s")
