import re

from buidl.hd import HDPublicKey, is_valid_bip32_path
from buidl.helper import sha256, uses_only_hex_chars
from buidl.op import OP_CODE_NAMES_LOOKUP
from buidl.script import P2WSHScriptPubKey, WitnessScript

import json


DESCRIPTOR_INPUT_CHARSET = "0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~ijklmnopqrstuvwxyzABCDEFGH`#\"\\ "
DESCRIPTOR_CHECKSUM_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def calc_poly_mod(c, val):
    c0 = c >> 35
    c = ((c & 0x7FFFFFFFF) << 5) ^ val
    if c0 & 1:
        c ^= 0xF5DEE51989
    if c0 & 2:
        c ^= 0xA9FDCA3312
    if c0 & 4:
        c ^= 0x1BAB10E32D
    if c0 & 8:
        c ^= 0x3706B1677A
    if c0 & 16:
        c ^= 0x644D626FFD
    return c


def calc_core_checksum(output_descriptor):
    """
    A 40-bit (!) digest that bitcoin core uses for output descriptors
    """

    c = 1
    cls = 0
    clscount = 0
    for ch in output_descriptor:
        pos = DESCRIPTOR_INPUT_CHARSET.find(ch)
        if pos == -1:
            raise ValueError(
                f"Invalid character `{ch}` in output descriptor: {output_descriptor}"
            )
        c = calc_poly_mod(c, pos & 31)
        cls = cls * 3 + (pos >> 5)
        clscount += 1
        if clscount == 3:
            c = calc_poly_mod(c, cls)
            cls = 0
            clscount = 0
    if clscount > 0:
        c = calc_poly_mod(c, cls)
    for j in range(0, 8):
        c = calc_poly_mod(c, 0)
    c ^= 1

    ret = [None] * 8
    for j in range(0, 8):
        ret[j] = DESCRIPTOR_CHECKSUM_CHARSET[(c >> (5 * (7 - j))) & 31]
    return "".join(ret)


def is_valid_xfp_hex(string):
    return len(string) == 8 and uses_only_hex_chars(string)


def parse_full_key_record(key_record_str):
    """
    A full key record will come from your Coordinator and include a reference to change derivation.
    It will look something like this:
    [c7d0648a/48h/1h/0h/2h]tpubDEpefcgzY6ZyEV2uF4xcW2z8bZ3DNeWx9h2BcwcX973BHrmkQxJhpAXoSWZeHkmkiTtnUjfERsTDTVCcifW6po3PFR1JRjUUTJHvPpDqJhr/0/*'
    """

    key_record_re = re.match(
        r"\[([0-9a-f]{8})(.*?)\]([0-9A-Za-z]+)\/([0-9]+?)\/\*", key_record_str
    )
    if key_record_re is None:
        raise ValueError(f"Invalid key record: {key_record_str}")

    xfp, path, xpub, index_str = key_record_re.groups()
    # Note that we don't validate xfp/index because the regex already tells us they're composed of ints

    index_int = int(index_str)

    path = "m" + path
    if not is_valid_bip32_path(path):
        raise ValueError(f"Invalid BIP32 path {path} in key record: {key_record_str}")

    try:
        parent_pubkey_obj = HDPublicKey.parse(s=xpub)
        is_testnet = parent_pubkey_obj.testnet
        xpub_child = parent_pubkey_obj.child(index=index_int).xpub()
    except ValueError:
        raise ValueError(f"Invalid xpub {xpub} in key record: {key_record_str}")

    return {
        "xfp": xfp,
        "path": path,
        "xpub_parent": xpub,
        "account_index": index_int,
        "xpub_child": xpub_child,
        "is_testnet": is_testnet,
    }


def parse_partial_key_record(key_record_str):
    """
    A partial key record will come from your Signer and include no references to change derivation.
    It will look something like this:
    [c7d0648a/48h/1h/0h/2h]tpubDEpefcgzY6ZyEV2uF4xcW2z8bZ3DNeWx9h2BcwcX973BHrmkQxJhpAXoSWZeHkmkiTtnUjfERsTDTVCcifW6po3PFR1JRjUUTJHvPpDqJhr
    """

    key_record_re = re.match(
        r"\[([0-9a-f]{8})\*?(.*?)\]([0-9A-Za-z].*)", key_record_str
    )
    if key_record_re is None:
        raise ValueError(f"Invalid key record: {key_record_str}")

    xfp, path, xpub = key_record_re.groups()
    # Note that we don't validate xfp because the regex already tells us it's hex

    path = "m" + path
    if not is_valid_bip32_path(path):
        raise ValueError(f"Invalid BIP32 path {path} in key record: {key_record_str}")

    try:
        pubkey_obj = HDPublicKey.parse(s=xpub)
        is_testnet = pubkey_obj.testnet
    except ValueError:
        raise ValueError(f"Invalid xpub {xpub} in key record: {key_record_str}")

    return {
        "xfp": xfp,
        "path": path,
        "xpub": xpub,
        "is_testnet": is_testnet,
    }


class P2WSHSortedMulti:

    # TODO: make an inheritable base descriptor class that this inherits from

    REQUIRE_SORTING_PUBKEYS = True
    DESCRIPTOR_TEXT_START = "wsh(sortedmulti("
    DESCRIPTOR_REGEX = r".*wsh\(sortedmulti\(([0-9]*),(.*)\)\)(\#[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{8})?.*"

    def __init__(
        self,
        quorum_m,  # m as in m-of-n
        key_records=[],  # pubkeys required to sign
        checksum="",
    ):
        if type(quorum_m) is not int or quorum_m < 1:
            raise ValueError(f"quorum_m must be a positive int: {quorum_m}")
        self.quorum_m = quorum_m

        if not key_records:
            raise ValueError("No key_records supplied")

        key_records_to_save, is_testnet = [], None
        descriptor_text = self.DESCRIPTOR_TEXT_START
        descriptor_text += f"{quorum_m}"
        for key_record in key_records:
            # TODO: does bitcoin core have a standard to enforce for h vs ' in bip32 path?
            path = key_record.get("path")
            if not is_valid_bip32_path(path):
                raise ValueError(
                    f"Invalid BIP32 path `{path}` in key record: {key_record}"
                )

            xfp_hex = key_record.get("xfp")
            if not is_valid_xfp_hex(xfp_hex):
                raise ValueError(
                    f"Invalid hex fingerprint `{xfp_hex}` in key record: {key_record}"
                )

            account_index = key_record.get("account_index")
            if type(account_index) is not int:
                raise ValueError(
                    f"Invalid account index `{account_index}` in key record: {key_record}"
                )

            xpub_parent = key_record.get("xpub_parent")
            try:
                hdpubkey_obj = HDPublicKey.parse(xpub_parent)
                # get rid of slip132 version byte (if it exists) as this will alter the checksum calculation
                hdpubkey_obj_attrs = vars(hdpubkey_obj)
                del hdpubkey_obj_attrs["pub_version"]
                del hdpubkey_obj_attrs["_raw"]
                xpub_to_use = HDPublicKey(**hdpubkey_obj_attrs).xpub()
            except ValueError:
                raise ValueError(
                    f"Invalid xpub_parent `{xpub_parent}` in key record: {key_record}"
                )

            if is_testnet is None:
                # This is the first key_record in our loop
                is_testnet = hdpubkey_obj.testnet
            else:
                # Validate that we haven't changed networks
                if hdpubkey_obj.testnet != is_testnet:
                    raise ValueError(
                        f"Network mismatch: testnet is set to {is_testnet} but xpub_parent is {xpub_parent}"
                    )

            key_records_to_save.append(
                {
                    "path": path,
                    "xfp": xfp_hex,
                    "xpub_parent": xpub_to_use,
                    "account_index": account_index,
                }
            )

            descriptor_text += f",[{key_record['xfp']}{key_record['path'][1:]}]{xpub_to_use}/{account_index}/*"

        descriptor_text += "))"
        self.descriptor_text = descriptor_text
        self.key_records = key_records_to_save
        self.is_testnet = is_testnet

        calculated_checksum = calc_core_checksum(descriptor_text)
        if checksum:
            # test that it matches
            if calculated_checksum != checksum:
                raise ValueError(
                    f"Calculated checksum `{calculated_checksum}` != supplied checksum `{checksum}`"
                )
        self.checksum = calculated_checksum

    def __repr__(self):
        return self.descriptor_text + "#" + self.checksum

    @property
    def quorum_n(self):
        return len(self.key_records)

    @property
    def m_of_n(self):
        return f"{self.quorum_m}-of-{self.quorum_n}"

    @classmethod
    def parse(cls, output_record):
        # Fix strange slashes that some software (Specter-Desktop) may export
        output_record = output_record.strip().replace(r"\/", "/")

        # Regex match the string
        re_output_results = re.match(
            cls.DESCRIPTOR_REGEX,
            output_record,
        )
        if re_output_results is None:
            raise ValueError(f"Not a valid wsh sortedmulti: {output_record}")

        quorum_m_str, key_records_str, checksum = re_output_results.groups()

        if "#" in output_record:
            if checksum:
                # get rid of leading # from capture group
                # TODO: is there a more elegant way to do this?
                checksum = checksum[1:]
            else:
                err_msg = f"Could not parse checksum in output_record: {output_record}"
                err_msg += "\n\nPerhaps try again this with no checksum (no #foo at the end of your output_record)?"
                raise ValueError(err_msg)

        quorum_m_int = int(quorum_m_str)

        key_records = []
        for key_record_str in key_records_str.split(","):
            # A full key record will look something like this:
            # [c7d0648a/48h/1h/0h/2h]tpubDEpefcgzY6ZyEV2uF4xcW2z8bZ3DNeWx9h2BcwcX973BHrmkQxJhpAXoSWZeHkmkiTtnUjfERsTDTVCcifW6po3PFR1JRjUUTJHvPpDqJhr/0/*'
            key_records.append(parse_full_key_record(key_record_str))

        if quorum_m_int > len(key_records):
            raise ValueError(
                f"Malformed threshold {quorum_m_int}-of-{len(key_records)} (m must be less than n) in {output_record}"
            )

        return cls(quorum_m=quorum_m_int, key_records=key_records, checksum=checksum)

    def get_address(self, offset=0, is_change=False):
        """
        If is_change=True, then we display change addresses.
        If is_change=False we display receive addresses.
        """
        assert type(is_change) is bool, is_change
        assert type(offset) is int and offset >= 0, offset

        sec_hexes_to_use = []
        for key_record in self.key_records:
            hdpubkey = HDPublicKey.parse(key_record["xpub_parent"])
            if is_change is True:
                account = key_record["account_index"] + 1
            else:
                account = key_record["account_index"]
            leaf_xpub = hdpubkey.child(account).child(offset)
            sec_hexes_to_use.append(leaf_xpub.sec().hex())

        commands = [OP_CODE_NAMES_LOOKUP[f"OP_{self.quorum_m}"]]
        if self.REQUIRE_SORTING_PUBKEYS:
            # BIP67 lexicographical sorting for sortedmulti
            commands.extend([bytes.fromhex(x) for x in sorted(sec_hexes_to_use)])
        else:
            commands.extend([bytes.fromhex(x) for x in sec_hexes_to_use])

        commands.append(OP_CODE_NAMES_LOOKUP[f"OP_{len(self.key_records)}"])
        commands.append(OP_CODE_NAMES_LOOKUP["OP_CHECKMULTISIG"])
        witness_script = WitnessScript(commands)
        redeem_script = P2WSHScriptPubKey(sha256(witness_script.raw_serialize()))
        return redeem_script.address(testnet=self.is_testnet)

    def caravan_export(self, wallet_name="p2wsh", key_record_names=[]):
        if key_record_names and len(key_record_names) != len(self.key_records):
            raise ValueError(
                f"{len(self.key_records)} key records but only {len(key_record_names)} names supplied: {key_record_names}"
            )

        to_return = {
            "name": wallet_name,
            "addressType": "P2WSH",
            "network": "testnet" if self.is_testnet else "mainnet",
            "client": {"type": "public"},  # node connection instructions
            "quorum": {
                "requiredSigners": self.quorum_m,
                "totalSigners": len(self.key_records),
            },
            "extendedPublicKeys": [],
            "startingAddressIndex": self.key_records[0]["account_index"],
        }
        for cnt, key_record in enumerate(self.key_records):
            to_append = {
                "bip32Path": key_record["path"].lower().replace("h", "'"),
                "xpub": key_record["xpub_parent"],
                "xfp": key_record["xfp"],
            }
            if key_record_names:
                name = key_record_names[cnt]
            else:
                # Generic/deterministic name: "Seed A"
                seed_letter = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[cnt]
                name = f"Seed {seed_letter}"
            to_append["name"] = name
            to_return["extendedPublicKeys"].append(to_append)

        # TODO: pretty-print this:
        return json.dumps(to_return)


class P2WSHUnsortedMulti(P2WSHSortedMulti):
    """
    This class requires users to supply their pubkeys (key_records) in the correct order

    DO NOT USE THIS CLASS FOR NEW WALLETS.
    It is only for recovery of older wallets that predate sortedmulti.
    """

    REQUIRE_SORTING_PUBKEYS = False
    DESCRIPTOR_TEXT_START = "wsh(multi("
    DESCRIPTOR_REGEX = (
        r".*wsh\(multi\(([0-9]*),(.*)\)\)(\#[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{8})?.*"
    )

    def caravan_export(self, *args, **kwargs):
        raise NotImplementedError("Unsorted multisig should not be supported")
