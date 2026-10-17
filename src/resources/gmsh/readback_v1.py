"""Read one mesh file in a throw-away Gmsh and print what it holds (GN15).

``python3 readback_v1.py <file> [<json options>]`` prints one JSON line:
node count, element counts per Gmsh element type, surface element count and
physical group names.  It runs in its own process because some readers (MED
among them) merge into whichever model carries the name stored in the file,
which would change the model of the resident session.
"""
import json
import sys


def main(argv):
    import gmsh

    path = argv[0]
    options = json.loads(argv[1]) if len(argv) > 1 else {}
    gmsh.initialize(['', '-noenv'], readConfigFiles=False, run=False)
    gmsh.option.setNumber('General.Terminal', 0)
    try:
        for name, value in options.items():
            if isinstance(value, str):
                gmsh.option.setString(name, value)
            else:
                gmsh.option.setNumber(name, float(value))
        gmsh.model.add('readback')
        gmsh.merge(path)
        nodes = len(gmsh.model.mesh.getNodes()[0])
        by_type, surface = {}, 0
        for dim, tag in gmsh.model.getEntities():
            types, tags, _n = gmsh.model.mesh.getElements(dim, tag)
            for element_type, element_tags in zip(types, tags):
                by_type[int(element_type)] = (
                    by_type.get(int(element_type), 0) + len(element_tags))
                if dim == 2:
                    surface += len(element_tags)
        groups = sorted({gmsh.model.getPhysicalName(d, t)
                         for d, t in gmsh.model.getPhysicalGroups()
                         if gmsh.model.getPhysicalName(d, t)})
        print(json.dumps({'nodes': nodes, 'by_type': by_type,
                          'elements': sum(by_type.values()),
                          'surface_elements': surface, 'groups': groups}))
    except Exception as error:
        print(json.dumps({'error': '%s: %s' % (type(error).__name__, error)}))
    finally:
        gmsh.finalize()


if __name__ == '__main__':
    main(sys.argv[1:])
