# (c) Copyright 2021 Aaron Kimball

from elftools.elf.elffile import ELFFile
import importlib.resources as resources
import os
import serial
import arduino_dbg.binutils as binutils

_LOCAL_CONF_FILENAME = os.path.expanduser("~/.arduino_dbg.conf")
_DBG_CONF_FMT_VERSION = 1

_dbg_conf_keys = [
    "arduino.platform",
    "arduino.arch",
    "dbg.conf.formatversion",
]

def _load_conf_module(module_name, resource_name):
    """
        Open a resource (file) within a module with a '.conf' extension and treat it like python
        code; execute it in a sheltered environment and return the processed globals as a k-v map.

        We use this for Arduino Platform and cpu architecture (Arch) definitions.
    """
    if resource_name is None or len(resource_name) == 0:
        return None # Nothing to load.

    conf_resource_name = resource_name.strip() + ".conf"
    conf_text = resources.read_text(module_name, conf_resource_name)
    conf = {} # Create an empty environment in which to run the config code.
    try:
        exec(conf_text, conf, conf)
        del conf["__builtins__"] # Pull python internals from gloabls map we're using as config.
    except:
        # Error parsing/executing conf; return empty result.
        print("Error loading config profile: %s" % conf_resource_name)
        return None

    print("Loading config profile: %s; read %d keys" % (conf_resource_name, len(conf)))
    # conf is now populated with the globals from executing the conf file.
    return conf


class Debugger(object):
    """
        Main debugger state object.
    """

    def __init__(self, elf_name, port, baud=57600, timeout=1):
        self._conn = None
        self.reopen(port, baud, timeout)
        self.elf_name = elf_name
        self._sections = {}
        self._addr_to_symbol = {}
        self._symbols = {}

        # General user-accessible config.
        # Load latest config from a dotfile in user's $HOME.
        self._init_config_from_file()

        self._read_elf()

    def _init_config_from_file(self):
        """
            If the user has a config file (see _LOCAL_CONF_FILENAME) then initialize self._config
            from that.
        """
        new_conf = {}
        for k in _dbg_conf_keys:
            new_conf[k] = None

        # If we open a file it can overwrite this but we start with this non-None default val.
        new_conf["dbg.conf.formatversion"] = _DBG_CONF_FMT_VERSION

        # The loaded config will be a map named 'config' within an otherwise-empty environment
        init_env = {}
        init_env['config'] = {}

        if os.path.exists(_LOCAL_CONF_FILENAME):
            with open(_LOCAL_CONF_FILENAME, "r") as f:
                conf_text = f.read()
                try:
                    exec(conf_text, init_env, init_env)
                except:
                    # error parsing or executing the config file. 
                    print("Warning: error parsing config file '%s'" % _LOCAL_CONF_FILENAME)
                    init_env['config'] = {}

        loaded_conf = init_env['config']

        # Merge loaded data on top of our default config.
        for (k, v) in loaded_conf.items():
            new_conf[k] = v

        self._config = new_conf
        self._platform = {} # Arduino platform-specific config (filled from conf file)
        self._arch = {} # CPU architecture-specific config (filled from conf file)

        self._load_platform() # cascade platform def from config, arch def from platform.

    def __persist_conf_var(self, f, k, v):
        """
            Persist k=v in serialized form to the file handle 'f'.

            Can be called with k=None to serialize a nested value in a complex type.
        """

        if k:
            f.write(f'  {repr(k)}: ')

        if v is None or type(v) == str or type(v) == int or type(v) == float or type(v) == bool:
            f.write(repr(v))
        elif type(v) == list:
            f.write('[')
            for elem in v:
                self.__persist_conf_var(f, None, elem)
                f.write(", ")
            f.write(']')
        elif type(v) == dict:
            f.write("{")
            for (dirK, dirV) in v.items():
                self.__persist_conf_var(f, None, dirK) # keys in a dir can be any type, not just str
                f.write(": ")
                self.__persist_conf_var(f, None, dirV)
                f.write(", ")
            f.write("}")
        else:
            print("Warning: unknown type serialization '%s'" % str(type(v)))
            # Serialize it as an abstract map; filter out python internals and methods 
            objdir = dict([(dirK, dirV) for (dirK, dirV) in dir(v).items() if \
                (not dirK.startswith("__") and not dirK.endswith("__") and \
                not callable(getattr(v, dirK))) ])

            self.__persist_conf_var(f, None, objdir)

        if k:
            f.write(",\n")

    def _persist_config(self):
        """
            Write the current config out to a file to reload the next time we use the debugger.
        """
        with open(_LOCAL_CONF_FILENAME, "w") as f:
            f.write("config = {\n\n")
            for (k, v) in self._config.items():
                self.__persist_conf_var(f, k, v)
            f.write("\n}\n")


    def _load_platform(self):
        """
            If the arduino.platform key is set, use it to load the platform-specific config.
        """
        platform_name = self.get_conf("arduino.platform")
        if not platform_name:
            return
        new_conf = _load_conf_module("arduino_dbg.platforms", platform_name)
        if not new_conf:
            return

        self._platform = new_conf
        self.set_conf("arduino.arch", self._platform["arch"]) # Triggers refresh of arch config.


    def _load_arch(self):
        """
            If the arduino.arch key is set, use it to load the arch-specific config.
        """
        arch_name = self.get_conf("arduino.arch")
        if not arch_name:
            return
        new_conf = _load_conf_module("arduino_dbg.arch", arch_name)
        if not new_conf:
            return # Nothing to load.

        self._arch = new_conf


    def set_conf(self, key, val):
        """
            Set a key/value pair in the configuration map.
            Then process any triggers associated w/ that key.
        """
        if key not in _dbg_conf_keys:
            raise KeyError("Not a valid conf key: %s" % key)
        self._config[key] = val

        # Process triggers for specific keys
        if key == "arduino.platform":
            self._load_platform()
        if key == "arduino.arch":
            self._load_arch()

        self._persist_config() # Write changes to conf file.

    def get_conf(self, key):
        if key not in _dbg_conf_keys:
            raise KeyError("Not a valid conf key: %s" % key)
        return self._config[key]

    def get_full_config(self):
        """
            Return all user-configurable configuration key/val pairs.
            Does not include architecture or platform config.
        """
        return self._config.items()

    def get_arch_conf(self, key):
        """
            Return an architecture-specific property setting. These are read-only
            from outside the Debugger object. If the architecture is not set, or
            the architecture lacks the requested property definition, this returns None.
        """
        try:
            return self._arch[key]
        except KeyError:
            return None

    def get_platform_conf(self, key):
        """
            Return an Arduino platform-specific property setting. These are read-only
            from outside the Debugger object. If the platform name is not set, or
            the platform lacks the requested property definition, this returns None.
        """
        try:
            return self._platform[key]
        except KeyError:
            return None

    def get_full_arch_config(self):
        return self._arch.items()

    def get_full_platform_config(self):
        return self._platform.items()


    def close(self):
        """
            Close serial connection.
        """
        self._conn.close()
        self._conn = None


    def reopen(self, port, baud, timeout):
        """
          (Re)establish serial connection.
        """
        if self._conn is not None:
            self.close()

        if port is not None and port != '':
            self._conn = serial.Serial(port, baud, timeout=timeout)

    def is_open(self):
        return self._conn is not None and self._conn.is_open

    def sym_for_addr(self, addr):
        """
            Return the name of the symbol keyed to a particular address.
        """
        return self._addr_to_symbol[addr]


    def function_sym_by_pc(self, pc):
        """
            Given a $PC pointing somewhere within a function body, return the name of
            the symbol for the function.
        """
        for (addr, name) in self._addr_to_symbol.items():
            if addr > pc:
                continue # Definitely not this one.

            sym = self._symbols[name]
            if sym['st_info']['type'] != "STT_FUNC":
                continue # Not a function

            if addr + sym['st_size'] >= pc:
                return name # Found it.


    def breakpoint(self):
        """
            Break program execution and get the attention of the debug server.
        """
        pass

    def get_registers(self):
        """
            Get snapshot of system registers.
        """
        if len(self._arch) == 0:
            print("Warning: No architecture specified; cannot assign specific registers")
            register_map = [ "general_regs" ]
            num_general_regs = -1
        else:
            register_map = self._arch["register_list_fmt"]
            num_general_regs = self._arch["general_regs"]

        reg_values = self.send_cmd("r", self.RESULT_LIST)
        registers = {}
        idx = 0
        general_reg_num = 0
        for reg_name in register_map:
            if reg_name == "general_regs":
                # The next 'n' entries are r0, r1, r2...
                # The arch config tells us how many to pull from the list (with the `general_regs`
                # config value).
                if num_general_regs == -1: # Undefined architecture; take all of them.
                    last = len(reg_values)
                else:
                    last = num_general_regs + idx

                start_idx = idx
                for rval in reg_values[start_idx:last]:
                    registers["r" + str(general_reg_num)] = int(rval, base=16)
                    general_reg_num += 1
                    idx += 1
            else:
                # We have a specific named register to assign.
                registers[reg_name] = int(reg_values[idx], base=16)
                idx += 1

        return registers


    RESULT_SILENT = 0
    RESULT_ONELINE = 1
    RESULT_LIST = 2

    def send_cmd(self, dbg_cmd, result_type):
        """
            Send a low-level debugger command across the wire and return the results.
            @param dbg_cmd either a formatted command string or list of cmd and arguments.
            @param result_type an integer/enum specifying whether to expect 0, 1, or 'n'
            ($-terminated list) lines in response.
        """

        if type(dbg_cmd) == list:
            dbg_cmd = dbg_cmd.join(" ") + "\n"
        elif type(dbg_cmd) != str:
            dbg_cmd = str(dbg_cmd)

        if not dbg_cmd.endswith("\n"):
            dbg_cmd = dbg_cmd + "\n"

        if not self.is_open():
            print("Error: No debug server connection open")
            return None

        self._conn.write(dbg_cmd.encode("utf-8"))
        #print("--> %s" % dbg_cmd.strip())
        # TODO(aaron): add debug verbosity that enables these i/o lines.

        # TODO(aaron): if we get any ">..." lines we should print them immediately and disregard them
        # as a response to send_cmd.

        # TODO(aaron): Monitor for unprompted text console responses and print them while the user's
        # still at the prompt.

        if result_type == self.RESULT_SILENT:
            return None
        elif result_type == self.RESULT_ONELINE:
            return self._conn.readline().decode("utf-8").strip()
        elif result_type == self.RESULT_LIST:
            lines = []
            while True:
                thisline = self._conn.readline().decode("utf-8").strip()
                #print("<-- %s" % thisline.strip())
                if len(thisline) == 0:
                    continue
                elif thisline == "$":
                    break
                else:
                    lines.append(thisline.strip())
            return lines
        else:
            raise RuntimeError("Invalid 'result_type' arg (%d) sent to send_cmd" % result_type)




    def _read_elf(self):
        """
            Read the target ELF file to load debugging information.
        """
        if self.elf_name is None:
            print("No ELF filename given")
            return

        with open(self.elf_name, 'rb') as f:
            elf = ELFFile(f)

            for sect in elf.iter_sections():
                my_section = {}
                my_section["name"] = sect.name
                my_section["size"] = sect.header['sh_size']
                my_section["offset"] = sect.header['sh_offset']
                self._sections[sect.name] = my_section

                #print("Section: %s at offset 0x%.8x with size %d" % (sect.name,
                #    sect.header['sh_offset'], sect.header['sh_size']))

            syms = elf.get_section_by_name(".symtab")
            if syms is not None:
                for sym in syms.iter_symbols():
                    sym_type = sym.entry['st_info']['type']
                    if sym_type == "STT_NOTYPE" or sym_type == "STT_OBJECT" or sym_type == "STT_FUNC":
                        # This has a location worth memorizing
                        self._addr_to_symbol[sym.entry['st_value']] = sym.name
                        continue
                    self._symbols[sym.name] = sym


                #print("*** Symbols (.symtab)")
                #for sym in syms.iter_symbols():
                #    print("%s: %s" % (sym.name, sym.entry))

        # Sort the symbols by address
        self._addr_to_symbol = dict(sorted(self._addr_to_symbol.items()))
        for (addr, name) in self._addr_to_symbol.items():
            print("%08x => %s (%s)" % (addr, name, binutils.demangle(name)))

