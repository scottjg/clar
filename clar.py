#!/usr/bin/env python

from __future__ import with_statement
from string import Template
import re, fnmatch, os

VERSION = "0.10.0"

TEST_FUNC_REGEX = r"^(void\s+(test_%s__(\w+))\(\s*void\s*\))\s*\{"

EVENT_CB_REGEX = re.compile(
    r"^(void\s+clar_on_(\w+)\(\s*void\s*\))\s*\{",
    re.MULTILINE)

SKIP_COMMENTS_REGEX = re.compile(
    r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
    re.DOTALL | re.MULTILINE)

CLAR_HEADER = """
/*
 * Clar v%s
 *
 * This is an autogenerated file. Do not modify.
 * To add new unit tests or suites, regenerate the whole
 * file with `./clar`
 */
""" % VERSION

CLAR_EVENTS = [
    'init',
    'shutdown',
    'test',
    'suite'
]

def main():
    from optparse import OptionParser

    parser = OptionParser()

    parser.add_option('-c', '--clar-path', dest='clar_path')
    parser.add_option('-v', '--report-to', dest='print_mode', default='default')

    options, args = parser.parse_args()

    for folder in args or ['.']:
        builder = ClarTestBuilder(folder,
            clar_path = options.clar_path,
            print_mode = options.print_mode)

        builder.render()


class ClarTestBuilder:
    def __init__(self, path, clar_path = None, print_mode = 'default'):
        self.declarations = []
        self.suite_names = []
        self.callback_data = {}
        self.suite_data = {}
        self.event_callbacks = []

        self.clar_path = os.path.abspath(clar_path) if clar_path else None

        self.path = os.path.abspath(path)
        self.modules = [
            "clar_sandbox.c",
            "clar_fixtures.c",
            "clar_fs.c"
        ]

        self.modules.append("clar_print_%s.c" % print_mode)

        print("Loading test suites...")

        for root, dirs, files in os.walk(self.path):
            module_root = root[len(self.path):]
            module_root = [c for c in module_root.split(os.sep) if c]

            tests_in_module = fnmatch.filter(files, "*.c")

            for test_file in tests_in_module:
                full_path = os.path.join(root, test_file)
                test_name = "_".join(module_root + [test_file[:-2]])

                with open(full_path) as f:
                    self._process_test_file(test_name, f.read())

        if not self.suite_data:
            raise RuntimeError(
                'No tests found under "%s"' % path)

    def render(self):
        main_file = os.path.join(self.path, 'clar_main.c')
        with open(main_file, "w") as out:
            out.write(self._render_main())

        header_file = os.path.join(self.path, 'clar.h')
        with open(header_file, "w") as out:
            out.write(self._render_header())

        print ('Written Clar suite to "%s"' % self.path)

    #####################################################
    # Internal methods
    #####################################################

    def _render_cb(self, cb):
        return '{"%s", &%s}' % (cb['short_name'], cb['symbol'])

    def _render_suite(self, suite):
        template = Template(
r"""
    {
        "${clean_name}",
        ${initialize},
        ${cleanup},
        ${cb_ptr}, ${cb_count}
    }
""")

        callbacks = {}
        for cb in ['initialize', 'cleanup']:
            callbacks[cb] = (self._render_cb(suite[cb])
                if suite[cb] else "{NULL, NULL}")

        return template.substitute(
            clean_name = suite['name'].replace("_", "::"),
            initialize = callbacks['initialize'],
            cleanup = callbacks['cleanup'],
            cb_ptr = "_clar_cb_%s" % suite['name'],
            cb_count = suite['cb_count']
        ).strip()

    def _render_callbacks(self, suite_name, callbacks):
        template = Template(
r"""
static const struct clar_func _clar_cb_${suite_name}[] = {
    ${callbacks}
};
""")
        callbacks = [
            self._render_cb(cb)
            for cb in callbacks
            if cb['short_name'] not in ('initialize', 'cleanup')
        ]

        return template.substitute(
            suite_name = suite_name,
            callbacks = ",\n\t".join(callbacks)
        ).strip()

    def _render_event_overrides(self):
        overrides = []
        for event in CLAR_EVENTS:
            if event in self.event_callbacks:
                continue

            overrides.append(
                "#define clar_on_%s() /* nop */" % event
            )

        return '\n'.join(overrides)

    def _render_header(self):
        template = Template(self._load_file('clar.h'))

        declarations = "\n".join(
            "extern %s;" % decl
            for decl in sorted(self.declarations)
        )

        return template.substitute(
            extern_declarations = declarations,
        )

    def _render_main(self):
        template = Template(self._load_file('clar.c'))
        suite_names = sorted(self.suite_names)

        suite_data = [
            self._render_suite(self.suite_data[s])
            for s in suite_names
        ]

        callbacks = [
            self._render_callbacks(s, self.callback_data[s])
            for s in suite_names
        ]

        callback_count = sum(
            len(cbs) for cbs in self.callback_data.values()
        )

        return template.substitute(
            clar_modules = self._get_modules(),
            clar_callbacks = "\n".join(callbacks),
            clar_suites = ",\n\t".join(suite_data),
            clar_suite_count = len(suite_data),
            clar_callback_count = callback_count,
            clar_event_overrides = self._render_event_overrides(),
        )

    def _load_file(self, filename):
        if self.clar_path:
            filename = os.path.join(self.clar_path, filename)
            with open(filename) as cfile:
                return cfile.read()

        else:
            import zlib, base64, sys
            content = CLAR_FILES[filename]

            if sys.version_info >= (3, 0):
                content = bytearray(content, 'utf_8')
                content = base64.b64decode(content)
                content = zlib.decompress(content)
                return str(content, 'utf-8')
            else:
                content = base64.b64decode(content)
                return zlib.decompress(content)

    def _get_modules(self):
        return "\n".join(self._load_file(f) for f in self.modules)

    def _skip_comments(self, text):
        def _replacer(match):
            s = match.group(0)
            return "" if s.startswith('/') else s

        return re.sub(SKIP_COMMENTS_REGEX, _replacer, text)

    def _process_test_file(self, suite_name, contents):
        contents = self._skip_comments(contents)

        self._process_events(contents)
        self._process_declarations(suite_name, contents)

    def _process_events(self, contents):
        for (decl, event) in EVENT_CB_REGEX.findall(contents):
            if event not in CLAR_EVENTS:
                continue

            self.declarations.append(decl)
            self.event_callbacks.append(event)

    def _process_declarations(self, suite_name, contents):
        callbacks = []
        initialize = cleanup = None

        regex_string = TEST_FUNC_REGEX % suite_name
        regex = re.compile(regex_string, re.MULTILINE)

        for (declaration, symbol, short_name) in regex.findall(contents):
            data = {
                "short_name" : short_name,
                "declaration" : declaration,
                "symbol" : symbol
            }

            if short_name == 'initialize':
                initialize = data
            elif short_name == 'cleanup':
                cleanup = data
            else:
                callbacks.append(data)

        if not callbacks:
            return

        tests_in_suite = len(callbacks)

        suite = {
            "name" : suite_name,
            "initialize" : initialize,
            "cleanup" : cleanup,
            "cb_count" : tests_in_suite
        }

        if initialize:
            self.declarations.append(initialize['declaration'])

        if cleanup:
            self.declarations.append(cleanup['declaration'])

        self.declarations += [
            callback['declaration']
            for callback in callbacks
        ]

        callbacks.sort(key=lambda x: x['short_name'])
        self.callback_data[suite_name] = callbacks
        self.suite_data[suite_name] = suite
        self.suite_names.append(suite_name)

        print("  %s (%d tests)" % (suite_name, tests_in_suite))



CLAR_FILES = {
"clar.c" : r"""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""",
"clar_print_default.c" : r"""eJyFU01P4zAQPSe/YqgU1a5Cuadi98ap4rLaE6DIxA5YSu3InnQPK/479jgFB9FycuZ53vObj5QeBeoOjlZL6Abh2tFpg602Gln4AFQe285OBmuIsZ80qhPQWeMRulfhYJMujDgoz8v/ZcGiJP+k78qCpHu22lshlYRKJjXfQOUfzaqG+CJfvJCrZgp/UDhUMpAC+laWZ6rwrxNK+8/8XEkElHPWJeBcBQnKmB9YRt6Vn0YfTfJYkCunRuuwpVzPLlqnHPJtpsOp0x7d1GFKowTY0EF2T09CaCyHO6GHyamG+hokeO6q8k1TeWCV5/AQgko+wcM1hiOml0VBqte/qNAsjr2I4cpYkMp3To+o7YLS6yFnDNqE8U2HZ+W+6MzowhecFmHOS009+BfK0j2w+SJ7HK5u4f7vfs+D/DmdLJ0vp3N5f6yJTlm+5sl62Me0M1klCehD35X8uj+RsFsixMlWuuqC38SG37C+W0MD6+36B380Ifb9f0gmbjZgrB1hc7Pc3uTokrR4Dru6kA6DqGG73ZLwUbSDDlfCvYw7Cn38KVmMa0gzK479XJ5HGWZBeE0UnjjKSDaHb+U7mrWGAw==""",
"clar_print_tap.c" : r"""eJyNVMFu2zAMPVtfwbgIYBu2gWK3BmuxnYthh+02wFBtORXmSIYkZxiG/vso2m6lJF12skk9ko+PlJh13MkWjlp20A7cNKORyjVSSZfhDzhhXdPqSbkSvG0n6cTqaLWyDtpnbqCYDxQ/CJuzPyzJfMr8LXy3ugLgiW/FEYU+S799+gpHYazUCm4//FBpvmMvjL1D2T5PrtO/1HXa3iGM0WZ2/A/d2BcE7xhLZA/ZJkqYvPZwAyO3VnTAhwG2HRHLbI7NlAFJbCwRgxVRYM/lgIEYxA9a7U+jg4IlxiVxtjXNbV1vu/Nq78tIaUlDNR3WEVtnptbNMAJAQZ9AOkR7Lda6AFVVzSMLfDhzy/cC7mBr35qo7udeDnYfw63A8Uv3+460OMtGowE4y0b+GOqbhwtQ74+RPYp+Cen9MXKQakV2IdL7G5TjSZh8XY/lqBO2NXJ0fqM3H+HL98fHcFkAAsApgeAoj5Wu6/ra5dCKVie8sLQP/hrOF2I2ifXsmNePJryW2lq/hNVCDIkvK/oAqdIO9M8UxUjx48/ChK8mlmMJ0SdyRozaLDtnsysd0Fizy29ORPMGiqJAkv5DCga4f5fgT0gnKoE7WXqBqcCRN4PEI272445MzIQB3i5hWd9+oWHxNZrwtUk/o0iAvxug/T2eAqiET5HPOYXqssV8YX8BFTvXlQ==""",
"clar_sandbox.c" : r"""eJyNVV1P20AQfLZ/xRIkYpNATItaVSkPlaBVVEoiEgQSRJaxz+SEfY7uLmkD4r931+fEHwRahBST3Zudmb0xSgeahxDOAgl+mATSnwd6dnvsffk07du2MmUutM2VvwwSHvk6nedNTpgJpc3RffrCtZ9tazz5NvEnoDSetngMDkE4VO7CntIu7JyA59qWJZleSAHeum9n7A/Gp4NLPHCotJ9mEXObfcWzE4QhU6pAvfaHP104Idi+/VLjHHNR5ZszvV/EMZNdUPyJ+RoSJh4M9V0ei4jF4F8PLj5+sK0Cx6gsupdoUJgthIYTOO43egw+E0s0SqrbKfagIVZr8muEulpdoKf848x8Xo3PLkeXw++D87OWDdYLSgSrmMRJb5xJcDjieH3g8LUc34dOh7s5fGM2Nj8wjQ/OhgifojGWMRm/JFPplOZiwWhKXnm9Xmo1I1CmFOF85ay9w1J37RxBV5ZkWS82/tpWbx8GMegZo24uM5EytC3KmBJt9DNYQSBWesbFQxe0XIHOYKEY9HA+7PfsN0i1qN4qeDVpmWKNWYUYktpliWIG+gfTE5bORwTqnF4PL09dc6wLBq5x+XaZiHhsdE1mXIFaKc3SjaCEPzIUUNNC4sOFlLlwLlmoMyy+I+7wTWWH78la/3lwVA3AMuMR5JFeCBWI6D7749B3eUyJQCXv3pQC1L7z2qVqvBoYiWoiwhmqQJZIs2JIrHyZVsCaKUQ/eRL5BQWjdMOjcnup4OuAJ3lyWjkeWXOT/7QobZvIrl8a9YCXHEy8s7hKy8UAVd885JZtIRhOQ7/xoS6iqf4ZcPUikyku7YnldGnRo+F4cAOY1N+BjEAlgZoxlS+5EmXrVZRJRBni5j54sY+7fB+W1ShBu9feRG2ziAYGKTuAoym9cbHfDKrXO50SjO7R+tqVXdAhpt1yOducxTHYtMUyYpQ+Ykzmvvrndhr/GMx6DAJdu+px77PnbT1QCTieosE1nujpxdX5+atDhYFlquoXOEf4/wjB3t62O7/9/hGKyVWV6FYvavT+AhbcW38=""",
"clar_fixtures.c" : r"""eJyFUV1LwzAUfW5+xZU9rLUVJ4ggZQ9DFAUfZEwQSglZmrBAl5Qkk6n43236tWbKfMvNOfecc+81llhBgSppLNAN0XCOuNjbnWa4InYTjpE1MSzxuD1Vki2L0BcKTKfn0EYgu57d3uRpjYhPhi1opSwumUwRCvo3zMFYXT9C5xA5stWSVh9hI5FAa+wUFG//osgJCA5tmQ1SF3CVw9kcppfTCAWBj8ZxDg3UN4/zZ7MaHBrHSBw7vpcJ4mGS5Ijtai9qnannNqk1q7myXU+KvhGaCF4wDnfPiyV+eHpbvS7v8cti9YjGq6Yl7lzCkxfo1L0j/lJOwOtrUrwrUcDBBRsii7Xan3bjBlNVL2WUzuMkgGlJdLuIP21oyYjcVf/a6G3ozXTQPRqmsZkwWQiOfgAVGffP""",
"clar_fs.c" : r"""eJylVdtu20YQfSa/YkAD8TKWY8dJX6L0wXDEVqgsBhINN7UFhiGX1qIkl9hd+dLG/57ZCynJUWEkfZE0s7NnZufMGe2xsqAlpJfj6ZsT399DgzUUojhKo8npb3Mg+ud8PBlNE/hq/NP4LJ5G49n5aTKOp71zNJvFs4vx06DzPz6MZ6HvS5UplkO+zAS89EtWUd7KtM3UkuS8kcqdGE/o/+t71tYm/ArTi8lk6HuS/UNTBRVtbtRyAGzo+x4rgaQ2zMaFvucJqlaicdd8z15AHKkE/rbxIQI6+DqrKp4TF3YAJ2GH/AxwTeu8fTBRA0jtl0Xp0K+sucAsx9suzPPauX2v5AIIMxYweO9AhnBwwELAbvTFXLGFrmf/aF+X4/Uu2L++3scEjwjmitRnQ/+x7/0tZ0XXecIaBTUv6AC22i/5SuRPnQWVynAy/z3CSYg/zpPZxVkCJQLp4m2YvYqVbJHrEHU7bJgG+y7IZNBQf1HBz2nNxQN5oeEHoDnnJdlOHYa2aa18dRetmlxziI8ZOl8bCV5ruk3u3ptw9OlUnaeMquxGorOfd/OcKs2kpEKlBFuMibHUuKUCm8gbW1aoOTge4HFwyZqC30l4EgdlhmYR+J4tVVBK1q0wpnv0U4JkKmqygxTDQEdfFKcfRpNRMsKx6zgzM7oLL+c4oz9A80aSs/jjp40U6bpmA46t0vgVzZpVS7TLApg3lOwe55A6ivMqE04hwcsgtCB7tJK0KxdH0pdLWlUpXylii3IVZuLm9mphsPXg6gsrqeXECtwH+Kl7jF96sLj4m6z1i773cGw1VLYCb5dEqoIKodnzgvmDVLQGtLl4B5/t7c+Q40ZwFL66bgLNmUfvmSKHr0Onsg5eT4LFp/c0vyWm1uPFwBTdBd9lTGGwvjCAF7b+Ad4b9mq9HP05TubJaXIxJ/b8f3DZU2lNU9Ivi+G2VNcL1dopLh3dt17IuC0LpHVDwuvA9TLtT21LrHm1EXlo9ly/s/4rwC5C1z00g6MvrDnK22DovCYoOJz1jpPFpsaN6412udkJndTNwdtF/zdiFF6vpMJxlNKIfD12hjQj7MiwD4qD7jkovbfcSEvtlVlTfOH3uxX+rKg3NL3B0dvFrh6I+rselNtN6F68oxk/+2araVBLuv3SZ6RvZL5q3BVi9r52bTgeUfZNwUr/G9kaoSs=""",
"clar.h" : r"""eJy9VU1P4zAQPZNfYZo9JJUFlCMLSAi1AqlCKyjavVmO4xBrEyfYztLVav874yRtmq922QOX1pnxzHvOe+O4IpIhjxAht8ubR7KaP63IHSGOC0EheS/uuEKypAg5utQmTERwEl87zq9MhIglVBFCtebKeM6RkAaxTIbCiExi5wjWGiIxVWgaiYTjaksCKJ0sVypTnVjINVMir3vZQh1nRRISGmTK+F8HOBD+WtCEaG+3Dx5/gKa9ADQe6ys8WzBUNNRl04ZobghLOJVF7pUxb1o/+tXz1MeoWmQ5fS14Q4FEulVq27oisvKVIi3uf6yeH+fk283qztnlYEvF2hSKe20VyhiRNG2h1GFNZRhk64+UbNjtKXE5WCJynNPp1EFTdFO+UlAVpZSpTKM3YWLE13kimDCotAJKudb0hcP+060xATUttCE5iEI8KFAYWZP4bR+WGR9dX6EzDGZe3C/nhNjV8v6hXE0WhWQlAUaTBEUUrBleoAlym54YzfwesN15GPhyFHe+zjkzPERRi4DJSg4HGNROPAh/PH5uwFfwXi2w0EhmBhlV8CHcjVa3MWc//0MnZus+Sagzv4/8yUoNUfgEoc78A0Mls38cp5rS0IQ9PC+Xw6PQKdp9572i+ujbirabq+3jpjt0jsZuDULfgj1SjVe6ZXvPUm7pVgyeZJEpZk0E3eA+PH2jSgr50mVfEhjwyZg7Vhxu2moYTibDl0WN9JGu36sSFBbK/hkLwtecFdZVF5MBz61+53A42nFe93SdL7OeYX3eprTNQdLHHqTxluGW4OTJlLxSoVNqWFwOg57BL8yRXZ6PXJjbT/cMi2Fg4UESgMUgsCsaELEfJPCCGQ7GQI6PIe1j+zcMFDRAwX6g3MtnOD/fmSQPIj66ukIehHcksiqm3MRZCPpZWtRKVYn05Q9fG64k2c38dTbf63eIKlZw"""
}
if __name__ == '__main__':
    main()
