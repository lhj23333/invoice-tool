import java.nio.file.Files;
import java.nio.file.Path;
import org.ofdrw.converter.export.CanvasImageExporter;
import org.ofdrw.reader.ContentExtractor;
import org.ofdrw.reader.OFDReader;

public final class InvoiceOFD {
    public static void main(String[] args) throws Exception {
        try (OFDReader reader = new OFDReader(Path.of(args[0]))) {
            if (reader.getNumberOfPages() > 20) {
                System.exit(2);
            }
            if (args.length == 3 && "--text".equals(args[2])) {
                ContentExtractor extractor = new ContentExtractor(reader);
                try (var output = Files.newBufferedWriter(Path.of(args[1]))) {
                    for (int page = 1; page <= reader.getNumberOfPages(); page++) {
                        for (String line : extractor.getPageContent(page)) {
                            output.write(line);
                            output.newLine();
                        }
                    }
                }
                return;
            }
            try (CanvasImageExporter exporter = new CanvasImageExporter(reader, Path.of(args[1]), "PNG", 12)) {
                exporter.export();
            }
        }
    }
}
