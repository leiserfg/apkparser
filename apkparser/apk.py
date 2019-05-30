import io
import logging
import re
import zipfile
from io import BytesIO
from struct import unpack
from zlib import crc32

from cryptography import x509
from cryptography.hazmat.backends import default_backend

# Used for reading Certificates
from pyasn1.codec.der.decoder import decode
from pyasn1.codec.der.encoder import encode

from . import util
from .axml import ARSCParser, ARSCResTableConfig, AXMLPrinter
from lxml import objectify, etree
from .build_icon import build_icon

NS_ANDROID_URI = "http://schemas.android.com/apk/res/android"
NS_ANDROID = "{http://schemas.android.com/apk/res/android}"

log = logging.getLogger("APKParser")


class Error(Exception):
    """Base class for exceptions in this module."""

    pass


class FileNotPresent(Error):
    pass


class BrokenAPKError(Error):
    pass


_xml_clean = etree.XSLT(
    etree.fromstring(
        """
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output indent="yes"/>
  <xsl:strip-space elements="*"/>

  <xsl:template match="node()">
    <xsl:copy>
      <xsl:apply-templates select="@*|node()"/>
    </xsl:copy>
  </xsl:template>

  <xsl:template match="*" priority="1">
    <xsl:element name="{local-name()}" namespace="">
      <xsl:apply-templates select="@*|node()"/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="@*">
    <xsl:attribute name="{local-name()}" namespace="">
      <xsl:value-of select="."/>
    </xsl:attribute>
  </xsl:template>
</xsl:stylesheet>
"""
    )
)


class APK(object):
    # Constants in ZipFile
    PK_END_OF_CENTRAL_DIR = b"\x50\x4b\x05\x06"
    PK_CENTRAL_DIR = b"\x50\x4b\x01\x02"

    # Constants in the APK Signature Block
    APK_SIG_MAGIC = b"APK Sig Block 42"
    APK_SIG_KEY_SIGNATURE = 0x7109871a

    def __init__(
        self, filename, raw=False, magic_file=None, skip_analysis=False, testzip=False
    ):
        """
            This class can access to all elements in an APK file

            :param filename: specify the path of the file, or raw data
            :param raw: specify if the filename is a path or raw data (optional)
            :param magic_file: specify the magic file (optional)
            :param skip_analysis: Skip the analysis, e.g. no manifest files are read. (default: False)
            :param testzip: Test the APK for integrity, e.g. if the ZIP file is broken. Throw an exception on failure (default False)

            :type filename: string
            :type raw: boolean
            :type magic_file: string
            :type skip_analysis: boolean
            :type testzip: boolean

            :Example:
              APK("myfile.apk")
              APK(read("myfile.apk"), raw=True)
        """
        self.filename = filename

        self.xml = {}
        self.axml = {}
        self.arsc = {}

        self.package = ""
        self.androidversion = {}
        self.permissions = []
        self.uses_permissions = []
        self.declared_permissions = {}
        self.app_icon = ""
        self.app_name = ""
        self.valid_apk = False
        self._is_signed_v2 = None
        self._v2_blocks = {}

        self._files = {}
        self.files_crc32 = {}

        self.magic_file = magic_file

        if raw is True:
            self.__raw = bytearray(filename)
        else:
            self.__raw = bytearray(util.read(filename))

        self.size = len(self.__raw)

        self.zip = zipfile.ZipFile(io.BytesIO(self.__raw), mode="r")

        if testzip:
            # Test the zipfile for integrity before continuing.
            # This process might be slow, as the whole file is read.
            # Therefore it is possible to enable it as a separate feature.
            #
            # A short benchmark showed, that testing the zip takes about 10 times longer!
            # e.g. normal zip loading (skip_analysis=True) takes about 0.01s, where
            # testzip takes 0.1s!
            ret = self.zip.testzip()
            if ret is not None:
                # we could print the filename here, but there are zip which are so broken
                # That the filename is either very very long or does not make any sense.
                # Thus we do not do it, the user might find out by using other tools.
                raise BrokenAPKError(
                    "The APK is probably broken: testzip returned an error."
                )

        if not skip_analysis:
            self._apk_analysis()

    def _apk_analysis(self):
        """
        Run analysis on the APK file.

        This method is usually called by __init__ except if skip_analysis is False.
        It will then parse the AndroidManifest.xml and set all fields in the APK class which can be
        extracted from the Manifest.
        """
        for i in self.zip.namelist():
            if i == "AndroidManifest.xml":
                self.axml[i] = AXMLPrinter(self.zip.read(i))
                self.xml[i] = None
                raw_xml = self.axml[i].get_buff()
                if len(raw_xml) == 0:
                    log.warning("AXML parsing failed, file is empty")
                else:
                    try:
                        if self.axml[i].is_packed():
                            log.warning(
                                "XML Seems to be packed, parsing is very likely to fail."
                            )

                        self.xml[i] = _xml_clean(self.axml[i].get_xml_obj()).getroot()

                    except Exception as e:
                        log.warning("reading AXML as XML failed: " + str(e))

                if self.xml[i] is not None:
                    self.package = self.xml[i].get("package")
                    self.androidversion["Code"] = self.xml[i].get("versionCode")
                    self.androidversion["Name"] = self.xml[i].get("versionName")

                    for item in self.xml[i].findall("uses-permission"):
                        name = item.get("name")
                        self.permissions.append(name)
                        maxSdkVersion = None
                        try:
                            maxSdkVersion = int(item.get("maxSdkVersion"))
                        except ValueError:
                            log.warning(
                                item.get("maxSdkVersion")
                                + "is not a valid value for <uses-permission> maxSdkVersion"
                            )
                        except TypeError:
                            pass
                        self.uses_permissions.append([name, maxSdkVersion])

                    # getting details of the declared permissions
                    for d_perm_item in self.xml[i].findall("permission"):
                        d_perm_name = self._get_res_string_value(
                            str(d_perm_item.get("name"))
                        )
                        d_perm_label = self._get_res_string_value(
                            str(d_perm_item.get("label"))
                        )
                        d_perm_description = self._get_res_string_value(
                            str(d_perm_item.get("description"))
                        )
                        d_perm_permissionGroup = self._get_res_string_value(
                            str(d_perm_item.get("permissionGroup"))
                        )
                        d_perm_protectionLevel = self._get_res_string_value(
                            str(d_perm_item.get("protectionLevel"))
                        )

                        d_perm_details = {
                            "label": d_perm_label,
                            "description": d_perm_description,
                            "permissionGroup": d_perm_permissionGroup,
                            "protectionLevel": d_perm_protectionLevel,
                        }
                        self.declared_permissions[d_perm_name] = d_perm_details

                    self.valid_apk = True

    def __getstate__(self):
        """
        Function for pickling APK Objects.

        We remove the zip from the Object, as it is not pickable
        And it does not make any sense to pickle it anyways.

        :return: the picklable APK Object without zip.
        """
        # Upon pickling, we need to remove the ZipFile
        x = self.__dict__
        x["axml"] = str(x["axml"])
        x["xml"] = str(x["xml"])
        del x["zip"]

        return x

    def __setstate__(self, state):
        """
        Load a pickled APK Object and restore the state

        We load the zip file back by reading __raw from the Object.

        :param state: pickled state
        """
        self.__dict__ = state

        self.zip = zipfile.ZipFile(io.BytesIO(self.__raw), mode="r")

    def _get_res_string_value(self, string):
        if not string.startswith("@string/"):
            return string
        string_key = string[9:]

        res_parser = self.get_android_resources()
        string_value = ""
        for package_name in res_parser.get_packages_names():
            extracted_values = res_parser.get_string(package_name, string_key)
            if extracted_values:
                string_value = extracted_values[1]
                break
        return string_value

    def is_valid_APK(self):
        """
            Return true if the APK is valid, false otherwise

            :rtype: boolean
        """
        return self.valid_apk

    def get_size(self):
        return self.size

    def get_filename(self):
        """
            Return the filename of the APK

            :rtype: string
        """
        return self.filename

    def get_name(self):
        """
            Return the appname of the APK

            :rtype: string
        """
        if not self.app_name:
            self.app_name = self.get_element("application", "label")

        if not self.app_name:
            self.app_name = self.get_element(
                "activity", "label", name=self.get_main_activity()
            )

        if not self.app_name:
            raise Exception("Error extracting application name.")

        if self.app_name.startswith("@"):
            res_id = int(self.app_name[1:], 16)
            res_parser = self.get_android_resources()

            try:
                self.app_name = res_parser.get_resolved_res_configs(
                    res_id, ARSCResTableConfig.default_config()
                )[0][1]
            except Exception as e:
                raise Exception('Error extracting application name "%s".' % e)

        return self.app_name

    def get_icon(self, max_dpi=65536):
        """
            Return the first non-greater density than max_dpi icon file name background and foreground,
            unless exact icon resolution is set in the manifest, in which case
            return the exact file

            From https://developer.android.com/guide/practices/screens_support.html
            ldpi (low) ~120dpi
            mdpi (medium) ~160dpi
            hdpi (high) ~240dpi
            xhdpi (extra-high) ~320dpi
            xxhdpi (extra-extra-high) ~480dpi
            xxxhdpi (extra-extra-extra-high) ~640dpi

            :rtype: string
        """

        if not self.app_icon:
            self.app_icon = self.get_element("application", "icon") or self.get_element(
                "activity", "icon", name=self.get_main_activity()
            )

        if self.app_icon.startswith("@"):
            self.app_icon = self._resolve_icon_resource(self.app_icon[1:], max_dpi)

        if not self.app_icon:
            raise Exception("Impossible to extract application icon.")

        return self.app_icon

    def _resolve_icon_resource(self, res, max_dpi):
        res_id = int(res, 16)
        res_parser = self.get_android_resources()
        candidates = res_parser.get_resolved_res_configs(res_id)

        res = None
        current_dpi = -1

        try:
            for config, file_name in candidates:
                dpi = config.get_density()
                if current_dpi < dpi <= max_dpi:
                    res = file_name
                    current_dpi = dpi
        except Exception as e:
            log.warning("Exception selecting application res: %s" % e)
        return res

    def extract_icon(self, filename, max_dpi=65536):
        """
        Extract application icon in `filename` location
        :param filename: 
        :return: 
        """
        icon = self.get_icon()
        if icon.endswith(".xml"):
            icon_element = AXMLPrinter(self.get_file(icon)).get_xml_obj()
            if icon_element.tag == 'adaptative-icon':
                parts = [
                    (icon_element.find("background").values())[0].replace(
                        "android:", ""
                    ),
                    (icon_element.find("foreground").values())[0].replace(
                        "android:", ""
                    ),
                ]
            else:
                # should be a bitmap
                parts = [icon_element.attrib.values()[0]]

            parts = [
                self._resolve_icon_resource(p[1:], max_dpi) if p.startswith("@") else p
                for p in parts
            ]

        else:
            parts = [icon]

        parts = filter(None, parts)  # some app have invalide parts :'(

        parts = [(p, None if p.startswith("#") else self.get_file(p)) for p in parts]
        build_icon(parts, filename)

    def get_package_name(self):
        """
            Return the name of the package
            :rtype: string
        """
        return self.package

    def get_version_code(self):
        """
            Return the application version code

            :rtype: string
        """
        return self.androidversion["Code"]

    def get_version_name(self):
        """
            Return the application version name

            :rtype: string
        """
        return self.androidversion["Name"]

    def get_files(self):
        """
            Return the files inside the APK

            :rtype: a list of strings
        """
        return self.zip.namelist()

    def _get_file_magic_name(self, buffer):
        """
        Return the filetype guessed for a buffer
        :param buffer: bytes
        :return: str of filetype
        """
        # TODO this functions should be better in another package
        default = "Unknown"
        ftype = None

        # There are several implementations of magic,
        # unfortunately all called magic
        try:
            import magic
        except ImportError:
            # no lib magic at all, return unknown
            return default

        try:
            # We test for the python-magic package here
            getattr(magic, "MagicException")
        except AttributeError:
            try:
                # Check for filemagic package
                getattr(magic.Magic, "id_buffer")
            except AttributeError:
                # Here, we load the file-magic package
                ms = magic.open(magic.MAGIC_NONE)
                ms.load()
                ftype = ms.buffer(buffer)
            else:
                # This is now the filemagic package
                if self.magic_file is not None:
                    m = magic.Magic(paths=[self.magic_file])
                else:
                    m = magic.Magic()
                ftype = m.id_buffer(buffer)
        else:
            # This is the code for python-magic
            if self.magic_file is not None:
                m = magic.Magic(magic_file=self.magic_file)
            else:
                m = magic.Magic()
            ftype = m.from_buffer(buffer)

        if ftype is None:
            return default
        else:
            return self._patch_magic(buffer, ftype)

    @property
    def files(self):
        """
        Wrapper for the files object

        :return: dictionary of files and their mime type
        """
        return self.get_files_types()

    def get_files_types(self):
        """
            Return the files inside the APK with their associated types (by using python-magic)

            :rtype: a dictionnary
        """
        if self._files == {}:
            # Generate File Types / CRC List
            for i in self.get_files():
                buffer = self.zip.read(i)
                self.files_crc32[i] = crc32(buffer)
                # FIXME why not use the crc from the zipfile?
                # crc = self.zip.getinfo(i).CRC
                self._files[i] = self._get_file_magic_name(buffer)

        return self._files

    def _patch_magic(self, buffer, orig):
        if ("Zip" in orig) or ("DBase" in orig):
            val = util.is_android_raw(buffer)
            if val == "APK":
                return "Android application package file"
            elif val == "AXML":
                return "Android's binary XML"

        return orig

    def get_files_crc32(self):
        """
        Calculates and returns a dictionary of filenames and CRC32

        :return: dict of filename: CRC32
        """
        if self.files_crc32 == {}:
            for i in self.get_files():
                buffer = self.zip.read(i)
                self.files_crc32[i] = crc32(buffer)

        return self.files_crc32

    def get_files_information(self):
        """
            Return the files inside the APK with their associated types and crc32

            :rtype: string, string, int
        """
        for k in self.get_files():
            yield k, self.get_files_types()[k], self.get_files_crc32()[k]

    def get_raw(self):
        """
            Return raw bytes of the APK

            :rtype: string
        """
        return self.__raw

    def get_file(self, filename):
        """
            Return the raw data of the specified filename
            inside the APK

            :rtype: string
        """
        try:
            return self.zip.read(filename)
        except KeyError:
            raise FileNotPresent(filename)

    def get_dex(self):
        """
            Return the raw data of the classes dex file

            :rtype: a string
        """
        try:
            return self.get_file("classes.dex")
        except FileNotPresent:
            return ""

    def get_dex_names(self):
        """
            Return the name of all classes dex files

            :rtype: a list of string 
        """
        dexre = re.compile("classes(\d*).dex")
        return filter(lambda x: dexre.match(x), self.get_files())

    def get_all_dex(self):
        """
            Return the raw data of all classes dex files

            :rtype: a generator
        """
        for dex_name in self.get_dex_names():
            yield self.get_file(dex_name)

    def is_multidex(self):
        """
        Test if the APK has multiple DEX files

        :return: True if multiple dex found, otherwise False
        """
        dexre = re.compile("^classes(\d+)?.dex$")
        return (
            len([instance for instance in self.get_files() if dexre.search(instance)])
            > 1
        )

    def get_elements(self, tag_name, attribute, with_namespace=True):
        """
        Return elements in xml files which match with the tag name and the specific attribute

        :param tag_name: a string which specify the tag name
        :param attribute: a string which specify the attribute
        """
        for i in self.xml:
            for item in self.xml[i].findall(".//" + tag_name):
                if with_namespace:
                    value = item.get(attribute)
                else:
                    value = item.get(attribute)
                # There might be an attribute without the namespace
                if value:
                    yield self.format_value(value)

    def format_value(self, value):
        """
        Format a value with packagename, if not already set

        :param value:
        :return:
        """
        if len(value) > 0:
            if value[0] == ".":
                value = self.package + value
            else:
                v_dot = value.find(".")
                if v_dot == 0:
                    value = self.package + "." + value
                elif v_dot == -1:
                    value = self.package + "." + value
        return value

    def get_element(self, tag_name, attribute, **attribute_filter):
        """
        Return element in xml files which match with the tag name and the specific attribute

        :param tag_name: specify the tag name
        :type tag_name: string
        :param attribute: specify the attribute
        :type attribute: string

        :rtype: string
        """
        for i in self.xml:
            if self.xml[i] is None:
                continue
            tag = self.xml[i].findall(".//" + tag_name)
            if len(tag) == 0:
                return None
            for item in tag:
                skip_this_item = False
                for attr, val in list(attribute_filter.items()):
                    attr_val = item.get(attr)
                    if attr_val != val:
                        skip_this_item = True
                        break

                if skip_this_item:
                    continue

                value = item.get(attribute)

                if value is not None:
                    return value
        return None

    def get_main_activity(self):
        """
            Return the name of the main activity

            :rtype: string
        """
        x = set()
        y = set()

        for i in self.xml:
            activities_and_aliases = self.xml[i].findall(".//activity") + self.xml[
                i
            ].findall(".//activity-alias")

            for item in activities_and_aliases:
                # Some applications have more than one MAIN activity.
                # For example: paid and free content
                activityEnabled = item.get("enabled")
                if (
                    activityEnabled is not None
                    and activityEnabled != ""
                    and activityEnabled == "false"
                ):
                    continue

                for sitem in item.findall(".//action"):
                    val = sitem.get("name")
                    if val == "android.intent.action.MAIN":
                        x.add(item.get("name"))

                for sitem in item.findall(".//category"):
                    val = sitem.get("name")
                    if val == "android.intent.category.LAUNCHER":
                        y.add(item.get("name"))

        z = x.intersection(y)
        if len(z) > 0:
            return self.format_value(z.pop())
        return None

    def get_activities(self):
        """
        Return the android:name attribute of all activities

        :rtype: a list of string
        """
        return list(self.get_elements("activity", "name"))

    def get_services(self):
        """
            Return the android:name attribute of all services

            :rtype: a list of string
        """
        return list(self.get_elements("service", "name"))

    def get_receivers(self):
        """
            Return the android:name attribute of all receivers

            :rtype: a list of string
        """
        return list(self.get_elements("receiver", "name"))

    def get_providers(self):
        """
            Return the android:name attribute of all providers

            :rtype: a list of string
        """
        return list(self.get_elements("provider", "name"))

    def get_intent_filters(self, category, name):
        d = {"action": [], "category": []}

        for i in self.xml:
            for item in self.xml[i].findall(".//" + category):
                if self.format_value(item.get("name")) == name:
                    for sitem in item.findall(".//intent-filter"):
                        for ssitem in sitem.findall("action"):
                            if ssitem.get("name") not in d["action"]:
                                d["action"].append(ssitem.get("name"))
                        for ssitem in sitem.findall("category"):
                            if ssitem.get("name") not in d["category"]:
                                d["category"].append(ssitem.get("name"))

        if not d["action"]:
            del d["action"]

        if not d["category"]:
            del d["category"]

        return d

    def get_permissions(self):
        """
            Return permissions

            :rtype: list of string
        """
        return self.permissions

    def get_uses_implied_permission_list(self):
        """
            Return all permissions implied by the target SDK or other permissions.

            :rtype: list of string
        """
        target_sdk_version = self.get_effective_target_sdk_version()

        READ_CALL_LOG = "android.permission.READ_CALL_LOG"
        READ_CONTACTS = "android.permission.READ_CONTACTS"
        READ_EXTERNAL_STORAGE = "android.permission.READ_EXTERNAL_STORAGE"
        READ_PHONE_STATE = "android.permission.READ_PHONE_STATE"
        WRITE_CALL_LOG = "android.permission.WRITE_CALL_LOG"
        WRITE_CONTACTS = "android.permission.WRITE_CONTACTS"
        WRITE_EXTERNAL_STORAGE = "android.permission.WRITE_EXTERNAL_STORAGE"

        implied = []

        implied_WRITE_EXTERNAL_STORAGE = False
        if target_sdk_version < 4:
            if WRITE_EXTERNAL_STORAGE not in self.permissions:
                implied.append([WRITE_EXTERNAL_STORAGE, None])
                implied_WRITE_EXTERNAL_STORAGE = True
            if READ_PHONE_STATE not in self.permissions:
                implied.append([READ_PHONE_STATE, None])

        if (
            WRITE_EXTERNAL_STORAGE in self.permissions or implied_WRITE_EXTERNAL_STORAGE
        ) and READ_EXTERNAL_STORAGE not in self.permissions:
            maxSdkVersion = None
            for name, version in self.uses_permissions:
                if name == WRITE_EXTERNAL_STORAGE:
                    maxSdkVersion = version
                    break
            implied.append([READ_EXTERNAL_STORAGE, maxSdkVersion])

        if target_sdk_version < 16:
            if (
                READ_CONTACTS in self.permissions
                and READ_CALL_LOG not in self.permissions
            ):
                implied.append([READ_CALL_LOG, None])
            if (
                WRITE_CONTACTS in self.permissions
                and WRITE_CALL_LOG not in self.permissions
            ):
                implied.append([WRITE_CALL_LOG, None])

        return implied

    @DeprecationWarning
    def get_requested_permissions(self):
        """
            Returns all requested permissions.

            :rtype: list of strings
        """
        return self.get_permissions()

    def get_declared_permissions(self):
        """
            Returns list of the declared permissions.

            :rtype: list of strings
        """
        return list(self.declared_permissions.keys())

    def get_declared_permissions_details(self):
        """
            Returns declared permissions with the details.

            :rtype: dict
        """
        return self.declared_permissions

    def get_max_sdk_version(self):
        """
            Return the android:maxSdkVersion attribute

            :rtype: string
        """
        return self.get_element("uses-sdk", "maxSdkVersion")

    def get_min_sdk_version(self):
        """
            Return the android:minSdkVersion attribute

            :rtype: string
        """
        return self.get_element("uses-sdk", "minSdkVersion")

    def get_target_sdk_version(self):
        """
            Return the android:targetSdkVersion attribute

            :rtype: string
        """
        return self.get_element("uses-sdk", "targetSdkVersion")

    def get_effective_target_sdk_version(self):
        """
            Return the effective targetSdkVersion, always returns int > 0.

            If the targetSdkVersion is not set, it defaults to 1.  This is
            set based on defaults as defined in:
            https://developer.android.com/guide/topics/manifest/uses-sdk-element.html

            :rtype: int
        """
        target_sdk_version = self.get_target_sdk_version()
        if not target_sdk_version:
            target_sdk_version = self.get_min_sdk_version()
        try:
            return int(target_sdk_version)
        except (ValueError, TypeError):
            return 1

    def get_libraries(self):
        """
            Return the android:name attributes for libraries

            :rtype: list
        """
        return self.get_elements("uses-library", "name")

    def get_certificate_der(self, filename):
        """
        Return the DER coded X.509 certificate from the signature file.

        :param filename: Signature filename in APK
        :return: DER coded X.509 certificate as binary
        """
        pkcs7message = self.get_file(filename)

        # TODO for correct parsing, we would need to write our own ASN1Spec for the SignatureBlock format
        message, _ = decode(pkcs7message)
        cert = encode(message[1][3])
        # Remove the first identifier
        # byte 0 == identifier, skip
        # byte 1 == length. If byte1 & 0x80 > 1, we have long format
        #                   The length of to read bytes is then coded
        #                   in byte1 & 0x7F
        # Check if the first byte is 0xA0 (Sequence Tag)
        tag = cert[0]
        l = cert[1]
        # Python2 compliance
        if not isinstance(l, int):
            l = ord(l)
            tag = ord(tag)
        if tag == 0xA0:
            cert = cert[2 + (l & 0x7F) if l & 0x80 > 1 else 2 :]

        return cert

    def get_certificate(self, filename):
        """
        Return a X.509 certificate object by giving the name in the apk file

        :param filename: filename of the signature file in the APK
        :return: a `x509` certificate
        """
        cert = self.get_certificate_der(filename)
        certificate = x509.load_der_x509_certificate(cert, default_backend())

        return certificate

    def new_zip(self, filename, deleted_files=None, new_files={}):
        """
            Create a new zip file

            :param filename: the output filename of the zip
            :param deleted_files: a regex pattern to remove specific file
            :param new_files: a dictionnary of new files

            :type filename: string
            :type deleted_files: None or a string
            :type new_files: a dictionnary (key:filename, value:content of the file)
        """
        zout = zipfile.ZipFile(filename, "w")

        for item in self.zip.infolist():
            # Block one: deleted_files, or deleted_files and new_files
            if deleted_files is not None:
                if re.match(deleted_files, item.filename) is None:
                    # if the regex of deleted_files doesn't match the filename
                    if new_files is not False:
                        if item.filename in new_files:
                            # and if the filename is in new_files
                            zout.writestr(item, new_files[item.filename])
                            continue
                    # Otherwise, write the original file.
                    buffer = self.zip.read(item.filename)
                    zout.writestr(item, buffer)
            # Block two: deleted_files is None, new_files is not empty
            elif new_files is not False:
                if item.filename in new_files:
                    zout.writestr(item, new_files[item.filename])
                else:
                    buffer = self.zip.read(item.filename)
                    zout.writestr(item, buffer)
            # Block three: deleted_files is None, new_files is empty.
            # Just write out the default zip
            else:
                buffer = self.zip.read(item.filename)
                zout.writestr(item, buffer)
        zout.close()

    def get_android_manifest_axml(self):
        """
            Return the :class:`AXMLPrinter` object which corresponds to the AndroidManifest.xml file

            :rtype: :class:`AXMLPrinter`
        """
        try:
            return self.axml["AndroidManifest.xml"]
        except KeyError:
            return None

    def get_android_manifest_xml(self):
        """
            Return the xml object which corresponds to the AndroidManifest.xml file

            :rtype: object
        """
        try:
            return self.xml["AndroidManifest.xml"]
        except KeyError:
            return None

    def get_android_resources(self):
        """
            Return the :class:`ARSCParser` object which corresponds to the resources.arsc file

            :rtype: :class:`ARSCParser`
        """
        try:
            return self.arsc["resources.arsc"]
        except KeyError:
            if "resources.arsc" not in self.zip.namelist():
                # There is a rare case, that no resource file is supplied.
                # Maybe it was added manually, thus we check here
                return None
            self.arsc["resources.arsc"] = ARSCParser(self.zip.read("resources.arsc"))
            return self.arsc["resources.arsc"]

    def is_signed(self):
        """
        Returns true if either a v1 or v2 (or both) signature was found.
        """
        return self.is_signed_v1() or self.is_signed_v2()

    def is_signed_v1(self):
        """
        Returns true if a v1 / JAR signature was found.
        Returning `True` does not mean that the file is properly signed!
        It just says that there is a signature file which needs to be validated.
        """
        return self.get_signature_name() is not None

    def is_signed_v2(self):
        """
        Returns true of a v2 / APK signature was found.
        Returning `True` does not mean that the file is properly signed!
        It just says that there is a signature file which needs to be validated.
        """
        if not self._is_signed_v2:
            # Need to find an v2 Block in the APK.
            # The Google Docs gives you the following rule:
            # * go to the end of the ZIP File
            # * search for the End of Central directory
            # * then jump to the beginning of the central directory
            # * Read now the magic of the signing block
            # * before the magic there is the size_of_block, so we can jump to
            # the beginning.
            # * There should be again the size_of_block
            # * Now we can read the Key-Values
            # * IDs with an unknown value should be ignored.
            f = io.BytesIO(self.__raw)

            size_central = None
            offset_central = None

            # Go to the end
            f.seek(-1, io.SEEK_END)
            # we know the minimal length for the central dir is 16+4+2
            f.seek(-20, io.SEEK_CUR)
            while f.tell() > 0:
                f.seek(-1, io.SEEK_CUR)
                r, = unpack("<4s", f.read(4))
                if r == self.PK_END_OF_CENTRAL_DIR:
                    # Read central dir
                    this_disk, disk_central, this_entries, total_entries, size_central, offset_central = unpack(
                        "<HHHHII", f.read(16)
                    )
                    # TODO according to the standard we need to check if the
                    # end of central directory is the last item in the zip file
                    # TODO We also need to check if the central dir is exactly
                    # before the end of central dir...

                    # These things should not happen for APKs
                    assert this_disk == 0, "Not sure what to do with multi disk ZIP!"
                    assert disk_central == 0, "Not sure what to do with multi disk ZIP!"
                    break
                f.seek(-4, io.SEEK_CUR)
            if offset_central:
                f.seek(offset_central)
                r, = unpack("<4s", f.read(4))
                f.seek(-4, io.SEEK_CUR)
                assert r == self.PK_CENTRAL_DIR, "No Central Dir at specified offset"

                # Go back and check if we have a magic
                end_offset = f.tell()
                f.seek(-24, io.SEEK_CUR)
                size_of_block, magic = unpack("<Q16s", f.read(24))
                self._is_signed_v2 = False
                if magic == self.APK_SIG_MAGIC:
                    # go back size_of_blocks + 8 and read size_of_block again
                    f.seek(-(size_of_block + 8), io.SEEK_CUR)
                    size_of_block_start, = unpack("<Q", f.read(8))
                    assert (
                        size_of_block_start == size_of_block
                    ), "Sizes at beginning and and does not match!"

                    # Store all blocks
                    while f.tell() < end_offset - 24:
                        size, key = unpack("<QI", f.read(12))
                        value = f.read(size - 4)
                        self._v2_blocks[key] = value

                    # Test if a signature is found
                    if self.APK_SIG_KEY_SIGNATURE in self._v2_blocks:
                        self._is_signed_v2 = True

        return self._is_signed_v2

    def get_certificates_der_v2(self):
        """
        Return a list of DER coded X.509 certificates from the v2 signature
        """
        # calling is_signed_v2 should also load the signature, if any
        if not self.is_signed_v2():
            return []

        certificates = []
        block_bytes = self._v2_blocks[self.APK_SIG_KEY_SIGNATURE]
        block = io.BytesIO(block_bytes)

        size_sequence, = unpack("<I", block.read(4))
        assert size_sequence + 4 == len(
            block_bytes
        ), "size of sequence and blocksize does not match"
        while block.tell() < len(block_bytes):
            size_signer, = unpack("<I", block.read(4))

            len_signed_data, = unpack("<I", block.read(4))
            len_digests, = unpack("<I", block.read(4))
            # Skip it for now
            block.seek(len_digests, io.SEEK_CUR)

            len_certs, = unpack("<I", block.read(4))
            start_certs = block.tell()
            while block.tell() < start_certs + len_certs:
                len_cert, = unpack("<I", block.read(4))
                certificates.append(block.read(len_cert))

            # Now we have the signatures and the public key...
            # we need to read it (or at least skip it)
            len_attr, = unpack("<I", block.read(4))
            block.seek(len_attr, io.SEEK_CUR)
            len_sigs, = unpack("<I", block.read(4))
            block.seek(len_sigs, io.SEEK_CUR)
            len_publickey, = unpack("<I", block.read(4))
            block.seek(len_publickey, io.SEEK_CUR)

        return certificates

    def get_certificates_v2(self):
        """
        Return a list of :class:`cryptography.x509.Certificate` which are found
        in the v2 signing block.
        Note that we simply extract all certificates regardless of the signer.
        Therefore this is just a list of all certificates found in all signers.
        """
        certs = []
        for cert in self.get_certificates_der_v2():
            certs.append(x509.load_der_x509_certificate(cert, default_backend()))

        return certs

    def get_signature_name(self):
        """
            Return the name of the first signature file found.
        """
        if self.get_signature_names():
            return self.get_signature_names()[0]
        else:
            # Unsigned APK
            return None

    def get_signature_names(self):
        """
        Return a list of the signature file names (v1 Signature / JAR
        Signature)

        :rtype: List of filenames matching a Signature
        """
        signature_expr = re.compile("^(META-INF/)(.*)(\.RSA|\.EC|\.DSA)$")
        signatures = []

        for i in self.get_files():
            if signature_expr.search(i):
                signatures.append(i)

        return signatures

    def get_signature(self):
        """
        Return the data of the first signature file found (v1 Signature / JAR
        Signature)

        :rtype: First signature name or None if not signed
        """
        if self.get_signatures():
            return self.get_signatures()[0]
        else:
            return None

    def get_signatures(self):
        """
        Return a list of the data of the signature files.
        Only v1 / JAR Signing.

        :rtype: list of bytes
        """
        signature_expr = re.compile("^(META-INF/)(.*)(\.RSA|\.EC|\.DSA)$")
        signature_datas = []

        for i in self.get_files():
            if signature_expr.search(i):
                signature_datas.append(self.get_file(i))

        return signature_datas
