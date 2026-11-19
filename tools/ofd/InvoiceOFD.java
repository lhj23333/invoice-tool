import java.nio.file.Path;
import org.ofdrw.converter.export.CanvasImageExporter;
import org.ofdrw.reader.OFDReader;

public final class InvoiceOFD {
    public static void main(String[] args) throws Exception {
        try (OFDReader reader = new OFDReader(Path.of(args[0]))) {
            if (reader.getNumberOfPages() > 20) {
                System.exit(2);
            }
            try (CanvasImageExporter exporter = new CanvasImageExporter(reader, Path.of(args[1]), "PNG", 12)) {
                exporter.export();
            }
        }
    }
}
